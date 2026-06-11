"""Warm-then-fan-out scheduling for provider prompt caches.

A provider-side prompt-cache entry only becomes readable after the first
request carrying that prefix has produced a response. inspect runs an eval's
samples (and epochs) concurrently, so N identical-prefix calls fired together
all miss the cache and pay full input price.

`gated_generate` is a drop-in replacement for inspect's `generate()` solver
that adds per-group leader election: samples carrying the same
`metadata["cache_group"]` key elect the first arrival as leader; followers
wait until the leader's model call returns (which writes the provider cache),
then proceed concurrently and read the now-warm prefix at the provider's
cache-read rate. Groups are independent — leaders of different groups still
run in parallel, so the wall-clock cost is roughly one extra call latency per
group.

Failure containment: the leader sets its group event in a ``finally`` block
(an errored leader never blocks followers), and followers carry a timeout
fallback after which they proceed ungated.
"""

from typing import TYPE_CHECKING, Any

from inspect_ai.solver import Generate, Solver, TaskState, solver

if TYPE_CHECKING:
    from inspect_ai.model import CachePolicy

__all__ = ["CACHE_GROUP_KEY", "gated_generate"]

# Sample metadata key carrying the cache-group id (samples sharing a provider
# prompt-cache prefix). Set by the generate/judge task builders.
CACHE_GROUP_KEY = "cache_group"

# Followers stop waiting for their leader after this long and proceed ungated.
# Generous: one judge/solver call, including provider retries.
DEFAULT_LEADER_TIMEOUT_S = 300.0


@solver
def gated_generate(
    cache: "CachePolicy | bool" = False,
    leader_timeout_s: float = DEFAULT_LEADER_TIMEOUT_S,
) -> Solver:
    """generate() with per-cache-group warm-then-fan-out scheduling.

    `cache` is inspect's local response-cache policy, passed through to
    `generate()` exactly as the built-in `generate(cache=...)` solver does.
    Samples without a `cache_group` metadata key generate immediately.
    """
    import anyio  # inspect_ai dependency; backend-agnostic events

    # Per-task-instance state: a fresh dict per eval (the solver factory runs
    # once per task build). Single event loop -> plain dict is race-free as
    # long as check-and-set happens without an intervening await.
    events: dict[Any, anyio.Event] = {}

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        group = (state.metadata or {}).get(CACHE_GROUP_KEY)
        if group is None:
            return await generate(state, cache=cache)
        event = events.get(group)
        if event is None:
            # Leader: write the provider cache; release the group no matter what.
            events[group] = anyio.Event()
            try:
                return await generate(state, cache=cache)
            finally:
                events[group].set()
        # Follower: wait for the leader's call to land, then fan out.
        with anyio.move_on_after(leader_timeout_s):
            await event.wait()
        return await generate(state, cache=cache)

    return solve
