"""Cache-aware execution scheduling: gate, ordering, split rubric, pricing."""

from types import SimpleNamespace

import anyio
import pandas as pd
import pytest

from itemeval._cachegate import CACHE_GROUP_KEY, gated_generate
from itemeval._config import ExperimentConfig, GraderSpec
from itemeval._item import Item
from itemeval._templates import Template
from itemeval._util import sha256_hex
from itemeval.budget._pricing import ModelPrice, anthropic_style_caching, cost_usd
from itemeval.design._grid import GradeCondition, expand_grade_grid
from itemeval.grade._judge import (
    JUDGE_FORMAT_SUFFIX,
    build_judge_input,
    build_judge_messages,
    build_judge_task,
)

# ---------------------------------------------------------------- fixtures


def _template(text: str, name: str = "rubric") -> Template:
    return Template(
        name=name, source="local", path=f"/x/{name}.md", text=text, sha256=sha256_hex(text.encode())
    )


RUBRIC = _template(
    "Grade this.\n\nProblem:\n{input}\n\nReference:\n{target}\n\nCandidate:\n{solution}\n\nBe strict."
)
ITEM = Item(id="p1", input="What is 2+2?", target="4", grading_scheme=None, metadata={})


def _judge_cond(split: bool = False) -> GradeCondition:
    return GradeCondition(
        id="judge-x",
        slug="judge_x",
        kind="judge",
        grader_name="j",
        grader_model="mockllm/judge",
        grader_max_tokens=512,
        rubric_name="rubric",
        rubric_hash=RUBRIC.hash12,
        split_rubric=split,
        payload={},
    )


def _pending(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["condition_id", "item_id", "epoch", "solution"])


# ------------------------------------------------------------ gate solver


def _state(group: "str | None") -> SimpleNamespace:
    md = {CACHE_GROUP_KEY: group} if group else {}
    return SimpleNamespace(metadata=md)


def test_gate_leader_completes_before_followers_start():
    order: list[str] = []

    async def main():
        solve = gated_generate(cache=False)
        started = []

        async def fake_generate(state, cache=False):
            tag = state.metadata.get("tag", "?")
            started.append(tag)
            order.append(f"start:{tag}")
            await anyio.sleep(0.05 if tag == "leader" else 0.0)
            order.append(f"end:{tag}")
            return state

        async def run(tag, group):
            s = _state(group)
            s.metadata["tag"] = tag
            await solve(s, fake_generate)

        async with anyio.create_task_group() as tg:
            tg.start_soon(run, "leader", "g1")
            await anyio.sleep(0.01)  # let the leader claim the group
            tg.start_soon(run, "f1", "g1")
            tg.start_soon(run, "f2", "g1")

    anyio.run(main)
    # the leader must fully finish before any follower starts
    assert order[0] == "start:leader"
    assert order.index("end:leader") < order.index("start:f1")
    assert order.index("end:leader") < order.index("start:f2")


def test_gate_groups_are_independent_and_ungrouped_passes_through():
    async def main():
        solve = gated_generate(cache=False)
        calls = []

        async def fake_generate(state, cache=False):
            calls.append(state.metadata.get(CACHE_GROUP_KEY))
            return state

        await solve(_state(None), fake_generate)
        await solve(_state("a"), fake_generate)
        await solve(_state("b"), fake_generate)
        assert calls == [None, "a", "b"]

    anyio.run(main)


def test_gate_failed_leader_releases_followers():
    async def main():
        solve = gated_generate(cache=False, leader_timeout_s=5.0)
        done = []

        async def failing_then_ok(state, cache=False):
            if state.metadata["tag"] == "leader":
                raise RuntimeError("boom")
            done.append(state.metadata["tag"])
            return state

        async def run_leader():
            s = _state("g")
            s.metadata["tag"] = "leader"
            with pytest.raises(RuntimeError):
                await solve(s, failing_then_ok)

        async def run_follower():
            s = _state("g")
            s.metadata["tag"] = "f"
            await solve(s, failing_then_ok)

        async with anyio.create_task_group() as tg:
            tg.start_soon(run_leader)
            await anyio.sleep(0.01)
            tg.start_soon(run_follower)
        assert done == ["f"]

    anyio.run(main)


# ------------------------------------------------- judge ordering + groups


def test_judge_dataset_sorted_by_item_and_grouped():
    pending = _pending(
        [
            ("genB", "p2", 1, "s3"),
            ("genA", "p1", 2, "s2"),
            ("genA", "p1", 1, "s1"),
        ]
    )
    items = {
        "p1": ITEM,
        "p2": Item(id="p2", input="x", target="y", grading_scheme=None, metadata={}),
    }
    task = build_judge_task(pending, items, _judge_cond(), RUBRIC, "study", cache=False)
    metas = [s.metadata for s in task.dataset]
    assert [m["item_id"] for m in metas] == ["p1", "p1", "p2"]
    assert [m["epoch"] for m in metas] == [1, 2, 1]
    assert all(m[CACHE_GROUP_KEY] == m["item_id"] for m in metas)


def test_judge_cache_schedule_off_has_no_groups():
    pending = _pending([("genA", "p1", 1, "s1")])
    task = build_judge_task(
        pending, {"p1": ITEM}, _judge_cond(), RUBRIC, "study", cache=False, cache_schedule=False
    )
    assert CACHE_GROUP_KEY not in task.dataset[0].metadata


# ------------------------------------------------------------ split rubric


def test_split_messages_concatenate_to_single_layout():
    single = build_judge_input(ITEM, "sol text", RUBRIC)
    msgs = build_judge_messages(ITEM, "sol text", RUBRIC)
    assert len(msgs) == 2
    assert msgs[0].role == "system" and msgs[1].role == "user"
    assert msgs[0].text + msgs[1].text == single
    assert "What is 2+2?" in msgs[0].text  # shared head holds the problem
    assert msgs[1].text.startswith("sol text")  # varying part starts at the solution
    assert msgs[1].text.endswith(JUDGE_FORMAT_SUFFIX)


def test_split_falls_back_to_single_message_without_shared_head():
    rubric = _template("{solution}\n\nGrade the above for {input}.")
    msgs = build_judge_messages(ITEM, "sol", rubric)
    assert len(msgs) == 1 and msgs[0].role == "user"


def test_split_task_uses_message_input():
    pending = _pending([("genA", "p1", 1, "s1")])
    task = build_judge_task(
        pending, {"p1": ITEM}, _judge_cond(split=True), RUBRIC, "study", cache=False
    )
    assert isinstance(task.dataset[0].input, list)


def test_split_rubric_changes_condition_id_only_when_enabled(tmp_path):
    base = {
        "study": "s",
        "benchmark": {
            "adapter": "hf",
            "datasets": [{"id": "org/ds"}],
            "mapping": {"input": "q"},
        },
        "solvers": {"models": ["mockllm/m"]},
        "facets": {"grader": ["j"]},
        "graders": {"j": {"model": "mockllm/judge"}},
    }
    cfg_plain = ExperimentConfig.model_validate(base)
    cfg_split = ExperimentConfig.model_validate(
        {**base, "graders": {"j": {"model": "mockllm/judge", "split_rubric": True}}}
    )
    rubrics = {"builtin:standard": RUBRIC}
    [plain] = expand_grade_grid(cfg_plain, rubrics)
    [split] = expand_grade_grid(cfg_split, rubrics)
    assert plain.id != split.id
    assert "layout" not in plain.payload and split.payload["layout"] == "split"
    assert split.split_rubric is True


# ------------------------------------------------------- config + pricing


def test_config_cache_knob_defaults():
    spec = GraderSpec(model="m/j")
    assert spec.split_rubric is False
    cfg = ExperimentConfig.model_validate(
        {
            "study": "s",
            "benchmark": {
                "adapter": "hf",
                "datasets": [{"id": "org/ds"}],
                "mapping": {"input": "q"},
            },
            "solvers": {"models": ["mockllm/m"]},
            "facets": {"scorer": "exact_match"},
        }
    )
    assert cfg.solvers.cache_prompt == "auto"
    assert cfg.budget.cache_schedule == "auto"


def test_anthropic_style_caching_detection():
    assert anthropic_style_caching("anthropic/claude-haiku-4-5")
    assert anthropic_style_caching("openrouter/anthropic/claude-haiku-4.5")
    assert anthropic_style_caching(None)
    assert not anthropic_style_caching("openai/gpt-5-mini")
    assert not anthropic_style_caching("openrouter/openai/gpt-5-mini")


def test_cache_write_pricing_is_provider_aware():
    price = ModelPrice(input_usd_per_mtok=10.0, output_usd_per_mtok=20.0)
    # anthropic-style: 1.25x write surcharge (also the model=None conservative default)
    anth = cost_usd(price, 0, 0, 0, 1_000_000, model="anthropic/claude-haiku-4-5")
    assert anth == pytest.approx(12.5)
    # token-prefix providers: writes free
    oai = cost_usd(price, 0, 0, 0, 1_000_000, model="openai/gpt-5-mini")
    assert oai == 0.0
    # explicit table rate always wins
    explicit = ModelPrice(
        input_usd_per_mtok=10.0, output_usd_per_mtok=20.0, cache_write_usd_per_mtok=2.0
    )
    assert cost_usd(explicit, 0, 0, 0, 1_000_000, model="openai/gpt-5-mini") == pytest.approx(2.0)


# ----------------------------------------------------------- split prompt


def test_split_prompt_renders_system_plus_user_and_changes_id():
    from itemeval.design._grid import expand_generate_grid
    from itemeval.generate._task import build_generate_task

    tmpl = _template("Long static instructions here.\n\nProblem:\n{input}\n\nAnswer briefly.", "p")
    base = {
        "study": "s",
        "benchmark": {
            "adapter": "hf",
            "datasets": [{"id": "org/ds"}],
            "mapping": {"input": "q"},
        },
        "solvers": {"models": ["mockllm/m"]},
        "facets": {"scorer": "exact_match", "prompt": ["p"], "replications": 3},
    }
    cfg_plain = ExperimentConfig.model_validate(base)
    cfg_split = ExperimentConfig.model_validate(
        {**base, "solvers": {"models": ["mockllm/m"], "split_prompt": True}}
    )
    [plain] = expand_generate_grid(cfg_plain, {"p": tmpl})
    [split] = expand_generate_grid(cfg_split, {"p": tmpl})
    assert plain.id != split.id
    assert "layout" not in plain.payload and split.payload["layout"] == "split"

    class Origin:
        dataset_id = "org/ds"
        revision = "r"

    task = build_generate_task([ITEM], split, tmpl, "s", 3, False, {"p1": Origin()})
    msgs = task.dataset[0].input
    assert isinstance(msgs, list) and len(msgs) == 2
    assert msgs[0].role == "system" and msgs[1].role == "user"
    single = (
        build_generate_task([ITEM], plain, tmpl, "s", 3, False, {"p1": Origin()}).dataset[0].input
    )
    assert msgs[0].text + msgs[1].text == single
    assert msgs[1].text.startswith("What is 2+2?")
