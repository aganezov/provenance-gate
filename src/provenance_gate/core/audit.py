"""Pure trust audit over the immutable Graph — the first verdict the gate computes.

Two deterministic, structural checks per computation, from provenance alone (nothing stored):

  STALE_INPUT   the node consumes a non-current version of some artifact (a newer version exists),
                *excluding* the node's own revision of that artifact — reading v1 to write v2 is a
                supersession, not stale use.
  VERSION_MIX   the node's input lineage reconverges on two *live* versions of one artifact — a
                divergent-branch merge (the moat trigger). A linear revision chain never mixes:
                a node that produces a version of an artifact SUBSUMES older versions of it in its
                forward cone, so only versions living on independent branches can collide.

Computed on read, never stored (the verdict is computed). One topological pass builds each node's
input cone, memoized per producer — we never re-walk the graph per node. The cone tracks, per
artifact_id, the set of *live* version ids reaching the node; >1 is a mix.

Entry points:
  audit_graph(graph)         -> {node_id: Verdict}   # the cockpit's per-node rail
  audit_inputs(graph, vids)  -> Verdict   # a planned / hypothetical node's inputs (the skill)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

from .model import ArtifactRef, Graph, Node

CLEAN = "clean"
STALE_INPUT = "stale_input"
VERSION_MIX = "version_mix"


@dataclass(frozen=True, slots=True)
class VersionIssue:
    """One artifact implicated in a verdict — its filename + the version numbers in play."""

    artifact: str                    # filename
    artifact_id: str
    versions: tuple[int, ...]        # the mixed set, or the single stale version
    current: Optional[int]           # the artifact's current version_number


@dataclass(frozen=True, slots=True)
class Verdict:
    """A node's computed trust judgment. ``level`` drives the rail; ``stale``/``mixed`` explain."""

    level: str = CLEAN  # clean | stale_input | version_mix (mix outranks stale)
    stale: tuple[VersionIssue, ...] = ()  # non-current direct inputs (excl. the node's revisions)
    mixed: tuple[VersionIssue, ...] = ()     # artifacts reaching the node at >1 live version


def _producer_map(graph: Graph) -> dict[str, str]:
    """version_id -> id of the node that produced it (each version has exactly one producer)."""
    return {a.artifact_version_id: n.id for n in graph.nodes for a in n.output_surface}


def _ref_map(graph: Graph) -> dict[str, ArtifactRef]:
    """version_id -> its ArtifactRef, taken from the producing node's output surface."""
    return {a.artifact_version_id: a for n in graph.nodes for a in n.output_surface}


def _deps(graph: Graph, producer_of: dict[str, str]) -> dict[str, set[str]]:
    """node_id -> the set of producer node ids it directly depends on (self-loops excluded)."""
    deps: dict[str, set[str]] = {}
    for n in graph.nodes:
        ps = {producer_of[r.artifact_version_id]
              for r in n.input_surface if r.artifact_version_id in producer_of}
        ps.discard(n.id)
        deps[n.id] = ps
    return deps


def _toposort(node_ids: list[str], deps: dict[str, set[str]]) -> list[str]:
    """Producers before consumers; ties broken by id for determinism (Kahn's)."""
    indeg = {n: 0 for n in node_ids}
    dependents: dict[str, list[str]] = {n: [] for n in node_ids}
    for n in node_ids:
        for p in deps[n]:
            if p in indeg:
                indeg[n] += 1
                dependents[p].append(n)
    q = deque(sorted(n for n in node_ids if indeg[n] == 0))
    order: list[str] = []
    while q:
        n = q.popleft()
        order.append(n)
        for c in sorted(dependents[n]):
            indeg[c] -= 1
            if indeg[c] == 0:
                q.append(c)
    if len(order) < len(node_ids):  # a provenance DAG shouldn't cycle; stay total if it does
        seen = set(order)
        order += sorted(n for n in node_ids if n not in seen)
    return order


def _out_cones(graph: Graph, deps: dict[str, set[str]]) -> dict[str, dict[str, set[str]]]:
    """One topological pass: node_id -> {artifact_id: {live version ids}} it carries forward.
    A node's outputs subsume older versions of the same artifact, so linear revisions collapse."""
    nodes = {n.id: n for n in graph.nodes}
    out: dict[str, dict[str, set[str]]] = {}
    for nid in _toposort([n.id for n in graph.nodes], deps):
        cone: dict[str, set[str]] = {}
        for pid in deps[nid]:
            if pid not in out:
                continue  # cycle recovery: predecessor not yet computed (DAG shouldn't cycle)
            for aid, vers in out[pid].items():
                cone.setdefault(aid, set()).update(vers)
        for a in nodes[nid].output_surface:      # produced version subsumes prior of same artifact
            cone[a.artifact_id] = {a.artifact_version_id}
        out[nid] = cone
    return out


def _in_cone(out_cones: dict[str, dict[str, set[str]]], producers: set[str]) -> dict[str, set[str]]:
    """Merge the out-cones of a node's input producers (before the node's own subsumption)."""
    ic: dict[str, set[str]] = {}
    for pid in producers:
        for aid, vers in out_cones[pid].items():
            ic.setdefault(aid, set()).update(vers)
    return ic


def _verdict(node: Node, in_cone: dict[str, set[str]], ref_of: dict[str, ArtifactRef]) -> Verdict:
    produced = {a.artifact_id for a in node.output_surface}
    # STALE_INPUT: non-current direct inputs, excluding an artifact the node itself revises
    stale = tuple(sorted(
        (
            VersionIssue(r.filename, r.artifact_id, (r.version_number,), r.latest_version_number)
            for r in node.input_surface
            if r.is_latest is False and r.artifact_id not in produced
        ),
        key=lambda s: s.artifact,
    ))
    # VERSION_MIX: an artifact reaching the node at >1 live version in its input lineage
    mixed = []
    for aid, vids in in_cone.items():
        if len(vids) < 2:
            continue
        refs = [ref_of[v] for v in vids if v in ref_of]
        nums = tuple(sorted({r.version_number for r in refs if r.version_number is not None}))
        if len(nums) > 1:
            mixed.append(VersionIssue(refs[0].filename, aid, nums, refs[0].latest_version_number))
    mixed = tuple(sorted(mixed, key=lambda m: m.artifact))
    level = VERSION_MIX if mixed else STALE_INPUT if stale else CLEAN
    return Verdict(level=level, stale=stale, mixed=mixed)


def audit_graph(graph: Graph) -> dict[str, Verdict]:
    """Per-node verdict for the whole graph, in one topological pass. Sources are always CLEAN."""
    producer_of = _producer_map(graph)
    ref_of = _ref_map(graph)
    deps = _deps(graph, producer_of)
    out_cones = _out_cones(graph, deps)
    return {n.id: _verdict(n, _in_cone(out_cones, deps[n.id]), ref_of) for n in graph.nodes}


def audit_inputs(graph: Graph, input_version_ids: list[str]) -> Verdict:
    """Verdict for a planned node consuming ``input_version_ids`` — the skill's pre-write audit.
    Reuses the cone machinery; the node produces nothing. Every id must resolve in ``graph`` — an
    unknown id would silently drop to a false ``CLEAN``, so we raise instead of trusting a partial
    verdict (the skill surfaces the gap)."""
    producer_of = _producer_map(graph)
    ref_of = _ref_map(graph)
    unresolvable = [v for v in input_version_ids if v not in ref_of]
    if unresolvable:
        raise ValueError(f"audit_inputs: version ids not in graph: {unresolvable}")
    deps = _deps(graph, producer_of)
    out_cones = _out_cones(graph, deps)
    planned = Node(
        id="\x00planned", cs_project_id=graph.cs_project_id, kind="computation", label="planned",
        input_surface=tuple(ref_of[v] for v in input_version_ids),
    )
    producers = {producer_of[v] for v in input_version_ids if v in producer_of}
    producers.discard(planned.id)
    return _verdict(planned, _in_cone(out_cones, producers), ref_of)
