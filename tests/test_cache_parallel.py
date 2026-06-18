"""Cache-saving mechanisms survive cross-condition parallel execution.

The warm-then-fan-out scheduler (`gated_generate`) elects a per-group leader so
followers read the warm provider cache. Its state is a closure dict created once
per task build, so two conditions that reuse the same group key (both keyed by
item id) must NOT collide when they now share a single inspect eval.
"""

from itemeval._cachegate import CACHE_GROUP_KEY
from itemeval.generate._task import build_generate_task


def _gen_task(prep, cond, *, cache_schedule):
    return build_generate_task(
        prep.items_effective,
        cond,
        prep.solver_templates[cond.prompt_name],
        prep.config.study,
        prep.plan.replications,
        prep.config.cache,
        prep.origins,
        cache_schedule=cache_schedule,
    )


def test_gate_engaged_with_reps_and_group_keys_overlap(study):
    """reps=2 -> gating on; both conditions key groups by the same item ids."""
    _, prep = study  # fixture: 2 models, 2 items, replications=2
    assert prep.plan.replications > 1
    c0, c1 = list(prep.grid.generate)[:2]
    t0, t1 = _gen_task(prep, c0, cache_schedule=True), _gen_task(prep, c1, cache_schedule=True)

    g0 = {s.metadata[CACHE_GROUP_KEY] for s in t0.dataset}
    g1 = {s.metadata[CACHE_GROUP_KEY] for s in t1.dataset}
    assert g0 and g0 == g1  # identical group-key sets — the collision scenario

    # ...yet the gate state is independent: a distinct solver closure per task,
    # so condition c1's "item-1" group can't be short-circuited by c0's leader.
    assert t0.solver is not t1.solver


def test_no_group_keys_when_scheduling_off(study):
    """cache_schedule off -> no gating metadata (plain generate, no leader wait)."""
    _, prep = study
    c0 = list(prep.grid.generate)[0]
    t0 = _gen_task(prep, c0, cache_schedule=False)
    assert all(CACHE_GROUP_KEY not in (s.metadata or {}) for s in t0.dataset)


def test_two_gated_solvers_have_independent_state():
    """Each gated_generate() call owns its own events dict (per-task isolation)."""
    from itemeval._cachegate import gated_generate

    assert gated_generate() is not gated_generate()
