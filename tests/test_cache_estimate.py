"""Cache-aware estimator (W3): per-group discount arithmetic, mock pricing only."""

import pandas as pd
import pytest

from conftest import TEST_CONFIG_YAML, write_study_files
from itemeval._templates import render_template
from itemeval._util import estimate_tokens
from itemeval.budget._estimator import estimate_study
from itemeval.budget._pricing import ModelPrice, PricingTable

# Prompt whose static head clears every provider minimum (5000 tokens at the
# chars/4 heuristic); items append a short tail.
BIG_HEAD = "y" * (4 * 5000)
BIG_PROMPT = BIG_HEAD + "\n{input}\n"

IN_RATE, READ_RATE = 1.0, 0.1  # USD per mtok; write defaults: 1.25x anth, 0 else


def _pricing(*models):
    return PricingTable(
        updated_at="t",
        source="file",
        models={m: ModelPrice(input_usd_per_mtok=IN_RATE, output_usd_per_mtok=0.0) for m in models},
    )


def _prep(tmp_path, model="openai/gpt-5-mini", extra_solver="", budget_extra=""):
    from itemeval._config import load_config
    from itemeval._prepare import prepare_study

    yaml_text = TEST_CONFIG_YAML.replace(
        "  models: [mockllm/solver-a, mockllm/solver-b]",
        f"  models: [{model}]" + extra_solver,
    )
    if budget_extra:
        yaml_text += budget_extra
    config = write_study_files(tmp_path, yaml_text)
    (tmp_path / "prompts" / "solver" / "minimal.md").write_text(BIG_PROMPT)
    prep = prepare_study(load_config(config))
    prep.pricing = _pricing(model, "anthropic/claude-haiku-4-5")
    return prep


def _prompt_tokens(prep):
    template = prep.solver_templates["minimal"].text
    return [
        estimate_tokens(render_template(template, {"input": it.input, "id": it.id}))
        for it in prep.items_effective
    ]


def test_token_prefix_monolithic_replication_groups(tmp_path, offline_adapter):
    """OpenAI, no split, reps=2: each item's epochs share the full prompt;
    free writes — followers read at 0.1x, leaders pay plain input."""
    prep = _prep(tmp_path)
    t1, t2 = _prompt_tokens(prep)
    est = estimate_study(prep)
    cond = est.generate.conditions[0]
    in_total = 2 * (t1 + t2)  # 2 epochs per item
    read = t1 + t2  # one follower per item
    assert cond.cache_read_tokens == read
    assert cond.cache_write_tokens == 0  # free-write provider
    expected = ((in_total - read) * IN_RATE + read * READ_RATE) / 1e6
    assert cond.usd == pytest.approx(expected)
    base = in_total * IN_RATE / 1e6
    assert cond.cache_discount_usd == pytest.approx(base - expected)
    # fresh study: remaining equals full, including the discount
    assert est.generate.remaining_usd == pytest.approx(expected)
    assert est.generate.remaining_cache_discount_usd == pytest.approx(base - expected)
    assert est.generate.cache_read_tokens == read


def test_anthropic_split_static_head_condition_group(tmp_path, offline_adapter):
    """Anthropic + split_prompt with a static head: one condition-wide group;
    the leader's write carries the 1.25x surcharge."""
    prep = _prep(
        tmp_path, model="anthropic/claude-haiku-4-5", extra_solver="\n  split_prompt: true"
    )
    head = estimate_tokens(BIG_HEAD + "\n")  # the template head runs up to {input}
    est = estimate_study(prep)
    cond = est.generate.conditions[0]
    calls = 4  # 2 items x 2 epochs, one group
    assert cond.cache_read_tokens == (calls - 1) * head
    assert cond.cache_write_tokens == head
    in_total = cond.input_tokens
    expected = (
        (in_total - calls * head) * IN_RATE + (calls - 1) * head * READ_RATE + head * 1.25 * IN_RATE
    ) / 1e6
    assert cond.usd == pytest.approx(expected)
    assert cond.cache_discount_usd > 0


def test_anthropic_write_surcharge_shown_honestly_on_tiny_judge_groups(tmp_path, offline_adapter):
    """Judge groups of size 1 (1 gen condition x 1 replication): write-only —
    the projection is MORE expensive than list, and the negative discount says so."""
    from itemeval._config import load_config
    from itemeval._prepare import prepare_study

    yaml_text = (
        TEST_CONFIG_YAML.replace("  replications: 2", "  replications: 1")
        .replace("  models: [mockllm/solver-a, mockllm/solver-b]", "  models: [mockllm/solver-a]")
        .replace(
            "    model: mockllm/judge",
            "    model: anthropic/claude-haiku-4-5\n    split_rubric: true",
        )
    )
    config = write_study_files(tmp_path, yaml_text)
    # rubric whose shared head (everything before {solution}) clears 4096 tokens
    (tmp_path / "rubrics" / "standard.md").write_text(
        BIG_HEAD + "\nProblem:\n{input}\n\nCandidate:\n{solution}\n"
    )
    prep = prepare_study(load_config(config))
    prep.pricing = _pricing("anthropic/claude-haiku-4-5", "mockllm/solver-a")
    est = estimate_study(prep)
    judge = next(c for c in est.grade.conditions if c.model == "anthropic/claude-haiku-4-5")
    assert judge.cache_read_tokens == 0  # no followers in a group of 1
    assert judge.cache_write_tokens > 0
    assert judge.cache_discount_usd < 0  # surcharge exceeds savings: shown honestly
    base = judge.input_tokens * IN_RATE / 1e6  # output rate is 0 in the fixture
    assert judge.usd > base


def test_batch_excludes_cache_discount(tmp_path, offline_adapter):
    prep = _prep(tmp_path, budget_extra="  policy: full-batch\n")
    est = estimate_study(prep)
    cond = est.generate.conditions[0]
    assert cond.batch_discount is True
    assert cond.cache_read_tokens == 0 and cond.cache_write_tokens == 0
    assert cond.cache_discount_usd == 0.0
    assert cond.usd == pytest.approx(cond.input_tokens * IN_RATE * 0.5 / 1e6)


def test_cache_schedule_off_excludes_discount(tmp_path, offline_adapter):
    prep = _prep(tmp_path, budget_extra="  cache_schedule: off\n")
    est = estimate_study(prep)
    cond = est.generate.conditions[0]
    assert cond.cache_read_tokens == 0 and cond.cache_discount_usd == 0.0


def test_prefix_below_provider_minimum_excludes_discount(tmp_path, offline_adapter):
    from itemeval._config import load_config
    from itemeval._prepare import prepare_study

    # default tiny prompt: full prompts well under OpenAI's 1024-token minimum
    yaml_text = TEST_CONFIG_YAML.replace(
        "  models: [mockllm/solver-a, mockllm/solver-b]", "  models: [openai/gpt-5-mini]"
    )
    config = write_study_files(tmp_path, yaml_text)
    prep = prepare_study(load_config(config))
    prep.pricing = _pricing("openai/gpt-5-mini")
    est = estimate_study(prep)
    cond = est.generate.conditions[0]
    assert cond.cache_read_tokens == 0 and cond.cache_discount_usd == 0.0


def test_single_replication_generate_never_projects_cache(tmp_path, offline_adapter):
    # the task builder only gates with >1 epoch; mirror it (reps=1 -> no projection)
    prep = _prep(tmp_path, extra_solver="")
    prep.config.facets.replications = 1
    prep.plan.replications = 1
    est = estimate_study(prep)
    assert est.generate.conditions[0].cache_read_tokens == 0


def test_delta_half_complete_group_is_followers_only(tmp_path, offline_adapter):
    """Item with one completed epoch: its group is warm — the remaining calls
    all read; the cold item still pays a leader."""
    prep = _prep(tmp_path)
    t1, t2 = _prompt_tokens(prep)
    cond_id = prep.grid.generate[0].id
    i1, i2 = (it.id for it in prep.items_effective)
    df = pd.DataFrame(
        [{"condition_id": cond_id, "item_id": i1, "epoch": 1, "solution": "s", "error": None}]
    )
    est = estimate_study(prep, df)
    st = est.generate
    in_total = 2 * (t1 + t2)  # both items re-run all epochs
    d_read = 2 * t1 + t2  # i1 warm: both epochs read; i2 cold: 1 follower
    expected = ((in_total - d_read) * IN_RATE + d_read * READ_RATE) / 1e6
    assert st.remaining_usd == pytest.approx(expected)
    assert st.remaining_cache_discount_usd == pytest.approx(in_total * IN_RATE / 1e6 - expected)
    # full-grid figures keep the cold-cache split
    assert st.cache_read_tokens == t1 + t2


def test_projection_line_shows_discount(tmp_path, offline_adapter, capsys, monkeypatch):
    """The pre-gate line carries the discount clause (JSON parity:
    StageEstimate.remaining_cache_discount_usd)."""
    from itemeval import cli

    yaml_text = TEST_CONFIG_YAML.replace(
        "  models: [mockllm/solver-a, mockllm/solver-b]", "  models: [openai/gpt-5-mini]"
    ).replace("  confirm_above_usd: 100", "  confirm_above_usd: 0")
    config = write_study_files(tmp_path, yaml_text)
    (tmp_path / "prompts" / "solver" / "minimal.md").write_text(BIG_PROMPT)
    rc = cli.main(["generate", str(config)])  # gate stops off-TTY: no API calls
    assert rc == 3
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("projected generate cost:"))
    assert "provider prompt-cache discount" in line
