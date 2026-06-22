"""Round-trip guard for the response-cache probe (cache-projection).

Pins itemeval's CacheEntry reconstruction to the installed inspect: run a real
(mock, cache-on) generate so inspect writes its response cache, then assert the
probe detects those exact calls as hits. If inspect changes `_cache_key`/
`CacheEntry`, this goes red — the documented bump checklist catches it instead of
the projection silently mis-counting. Hermetic: mockllm + the conftest's tmp
INSPECT_CACHE_DIR; no network.
"""

import yaml

from conftest import write_study_files

from itemeval import ExperimentConfig
from itemeval._cacheprobe import probe_generate, probe_grade
from itemeval._prepare import prepare_study
from itemeval.generate._run import run_generate
from itemeval.grade._run import run_grade


def _prep_with_cache(tmp_path, cache: bool):
    config_path = write_study_files(tmp_path)
    data = yaml.safe_load(config_path.read_text())
    data["cache"] = cache
    cfg = ExperimentConfig.model_validate(data)
    cfg._config_dir = config_path.parent
    cfg._work_dir = config_path.parent
    return cfg, prepare_study(cfg)


def test_probe_off_when_cache_disabled(tmp_path, offline_adapter):
    _, prep = _prep_with_cache(tmp_path, cache=False)
    probe = probe_generate(prep)
    assert probe.cache_hits == 0 and probe.cache_misses == 0 and probe.cache_dir is None


def test_probe_detects_real_cache_writes(tmp_path, offline_adapter):
    cfg, prep = _prep_with_cache(tmp_path, cache=True)
    # Cold cache: the early-out short-circuits before resolving anything (no entries
    # exist, so every call is a guaranteed miss — reported as no projection).
    cold = probe_generate(prep, force=True)
    assert cold.cache_hits == 0 and cold.cache_misses == 0 and cold.cache_dir is None

    # Run generate (cache on) so inspect writes its response cache for every call.
    run_generate(prep)

    # The same calls must now be detected as hits (force=True re-probes them all,
    # bypassing the store-resume skip so we test the cache layer specifically).
    # 2 gen conditions x 2 effective items x 2 reps = 8 calls.
    warm = probe_generate(prep, force=True)
    assert warm.cache_hits == 8 and warm.cache_misses == 0
    assert warm.cache_dir is not None


def test_probe_miss_on_changed_message(tmp_path, offline_adapter, monkeypatch):
    cfg, prep = _prep_with_cache(tmp_path, cache=True)
    run_generate(prep)
    # Mutate the rendered input: a different prompt -> a different cache key -> miss.
    # Patch the source module (the probe does `from generate._task import ...` at
    # call time, so it picks up the patched attribute).
    import itemeval.generate._task as task_mod

    real = task_mod.render_generate_input

    def perturbed(item, cond, template):
        out = real(item, cond, template)
        return (out + " X") if isinstance(out, str) else out

    monkeypatch.setattr(task_mod, "render_generate_input", perturbed)
    probe = probe_generate(prep, force=True)
    assert probe.cache_hits == 0 and probe.cache_misses > 0


def test_estimate_reports_cache_projection_on_force(tmp_path, offline_adapter):
    from itemeval.budget._estimator import estimate_study

    cfg, prep = _prep_with_cache(tmp_path, cache=True)
    run_generate(prep)  # warm the response cache
    est = estimate_study(prep, force=True)
    gen = est.generate
    # Every forced generate call is now cached → real remaining ~ $0, gate figure intact.
    assert gen.cache_hits == 8 and gen.cache_misses == 0
    assert gen.remaining_usd > 0  # the ceiling/gate figure is unchanged
    assert gen.real_remaining_usd == 0.0  # all calls cached -> nothing paid fresh


def test_estimate_cache_fields_zero_without_cache(tmp_path, offline_adapter):
    from itemeval.budget._estimator import estimate_study

    cfg, prep = _prep_with_cache(tmp_path, cache=False)
    run_generate(prep)
    gen = estimate_study(prep, force=True).generate
    assert gen.cache_hits == 0 and gen.cache_misses == 0
    assert gen.real_remaining_usd == gen.remaining_usd  # no projection applied


def test_cache_prompt_resolves_off_design_reps_not_policy():
    """`cache_prompt` rides in the response-cache key, so it must resolve off the
    design replication count — never the policy-adjusted plan — or a dev pilot and
    its full run get mismatched keys."""
    from itemeval.generate._params import resolve_cache_prompt

    assert resolve_cache_prompt("auto", 1) is None
    assert resolve_cache_prompt("auto", 2) is True
    assert resolve_cache_prompt("on", 1) is True
    assert resolve_cache_prompt("off", 2) is False


def test_dev_pilot_generations_replay_on_full_run(tmp_path, offline_adapter):
    """Regression (cache_prompt design-reps invariance): a dev pilot whose policy
    caps replications to 1 must cache its epoch-1 calls under the same key the full
    run (reps 2) reads, so the full run replays the pilot's generations instead of
    silently regenerating them — which re-pays and then staples the pilot's stale
    grades onto overwritten solutions. Pre-fix the pilot keyed on cache_prompt=None
    (plan reps=1) and the full run on True (plan reps=2): every epoch-1 call missed.
    """
    config_path = write_study_files(tmp_path)
    data = yaml.safe_load(config_path.read_text())
    data["cache"] = True
    data["budget"]["dev_replications"] = 1  # design is reps=2; the dev policy caps to 1
    cfg = ExperimentConfig.model_validate(data)
    cfg._config_dir = config_path.parent
    cfg._work_dir = config_path.parent

    prep_dev = prepare_study(cfg, policy="dev")
    assert prep_dev.plan.replications == 1  # the dev cap is active
    run_generate(prep_dev)  # caches epoch 1 for every pilot item

    prep_full = prepare_study(cfg, policy="full-interactive")
    assert prep_full.plan.replications == 2
    assert len(prep_full.items_effective) > len(prep_dev.items_effective)  # pilot is a subset
    # force=True probes every epoch: the pilot's epoch-1 calls are cache HITS (matching
    # key); the never-produced epoch-2 calls and the items beyond the pilot subset miss.
    probe = probe_generate(prep_full, force=True)
    n_gen = len(prep_full.grid.generate)
    pilot_calls = n_gen * len(prep_dev.items_effective)  # epoch-1 of the pilot items
    total_calls = n_gen * len(prep_full.items_effective) * prep_full.plan.replications
    assert probe.cache_hits == pilot_calls  # every pilot epoch-1 call replays ($0); pre-fix: 0
    assert probe.cache_misses == total_calls - pilot_calls  # genuinely fresh calls


def test_probe_grade_detects_real_judge_cache(tmp_path, offline_adapter):
    cfg, prep = _prep_with_cache(tmp_path, cache=True)
    run_generate(prep)
    cold = probe_grade(prep, force=True)
    assert cold.cache_hits == 0 and cold.cache_misses > 0
    run_grade(prep)
    warm = probe_grade(prep, force=True)
    assert warm.cache_hits == cold.cache_misses and warm.cache_misses == 0
