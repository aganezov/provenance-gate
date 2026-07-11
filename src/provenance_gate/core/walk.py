"""Pure transitive closure over the dependency graph — the seed set for a subgraph read.

An adapter (raw sqlite, or CS ``host.query``) supplies the one-hop ``expand`` — a frontier of
version ids mapped to the version ids one step in the chosen direction (upstream = the versions they
depend on; downstream = the versions that depend on them). This module only iterates that to a
fixpoint. Keeping the walk pure and reader-agnostic is what lets the external substrate and the
in-CS kernel share ONE traversal (and lets it inline into the skill kernel like derive/audit).

The direction and the SQL live in the adapter's ``expand``; ``max_depth`` is the only knob here.
``max_depth=None`` walks to the end of the frontier (the full cone).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Optional


def closure(
    seeds: Iterable[str],
    expand: Callable[[frozenset], set],
    *,
    max_depth: Optional[int] = None,
) -> set:
    """Transitive closure of ``seeds`` under ``expand`` — BFS to a fixpoint (or ``max_depth`` hops).

    ``expand(frontier)`` returns the version ids one hop out from the given frontier; only ids not
    yet seen advance, so a cyclic dependency graph still terminates. The seeds are always included.
    ``max_depth=0`` returns just the seeds; ``None`` runs to the end of the frontier.
    """
    seen: set = set(seeds)
    frontier: frozenset = frozenset(seen)
    depth = 0
    while frontier and (max_depth is None or depth < max_depth):
        nxt = expand(frontier) - seen  # only newly-seen ids advance — terminates on cycles
        seen |= nxt
        frontier = frozenset(nxt)
        depth += 1
    return seen
