"""Pure trust audit over the immutable Graph — the first verdict the gate computes.

Two deterministic, structural checks per computation, from provenance alone (nothing stored):

  STALE_INPUT   the node consumes a non-current version of some artifact (a newer version exists),
                *excluding* the node's own revision of that artifact — reading v1 to write v2 is a
                supersession, not stale use.
  VERSION_MIX   the node's input lineage reconverges on two *live* versions of one artifact — a
                divergent-branch merge (the moat trigger). A linear revision chain never mixes:
                a node that produces a version of an artifact SUBSUMES older versions of it in its
                forward cone, so only versions living on independent branches can collide.

Computed on read, never stored (the verdict is computed). One topological pass builds a cone per
PRODUCED VERSION (its own consumed lineage); a node's mix is judged over the merge of just what it
consumed — never a producing cell's sibling outputs, so a co-output can't fake a mix. The cone
tracks, per artifact_id, the set of *live* version ids reaching the node; >1 is a mix.

Entry points:
  audit_graph(graph)         -> {node_id: Verdict}   # the cockpit's per-node rail
  audit_inputs(graph, vids)  -> Verdict   # a planned / hypothetical node's inputs (the skill)
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
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
    """version_id -> its ArtifactRef. Producing output surfaces are authoritative; consumed input
    surfaces then fill in any version no node here produces (external / partial input), so the audit
    can still name it in a version_mix instead of silently dropping it."""
    m: dict[str, ArtifactRef] = {}
    for n in graph.nodes:
        for a in n.output_surface:
            m[a.artifact_version_id] = a
    for n in graph.nodes:
        for a in n.input_surface:
            m.setdefault(a.artifact_version_id, a)
    return m


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


def _merge_consumed(input_surface, vcones: dict) -> dict:
    """The mix cone of an input surface: merge each CONSUMED version's own lineage cone. A consumed
    version with no computed cone (a dangling / external input) seeds as its own source, using the
    input ref's OWN artifact_id, so it counts toward a mix even when no node here produces it. A
    node's version_mix is judged over only what it consumed, never a producing cell's siblings."""
    ic: dict[str, set[str]] = {}
    for r in input_surface:
        uc = vcones.get(r.artifact_version_id)
        if uc is None:
            uc = {r.artifact_id: {r.artifact_version_id}}
        for aid, vers in uc.items():
            ic.setdefault(aid, set()).update(vers)
    return ic


def _cones(graph: Graph, deps: dict[str, set[str]]):
    """One topological pass. Returns (vcones, in_cones):
      vcones[version_id] = {artifact_id: {live version ids}} in its consumed lineage: the version
        itself plus what its producing cell consumed. A cell's co-*output* siblings are excluded —
        they are co-produced peers, never inputs — so one a consumer never read can't fake a mix.
        But ALL of a cell's inputs are attributed to EACH of its outputs on purpose: the agent could
        reason over any consumed input to write any output, so the explicit per-file dep edges are a
        lower bound and we keep the safe over-approximation (conservative for a trust check).
      in_cones[node_id] = the merged lineage of what the node consumed (the cone its verdict reads).
    Each produced version subsumes older versions of its artifact, so a linear revision collapses.
    Assumes a DAG — always true for immutable-artifact provenance (a version can only depend on
    versions that already existed). A cycle (impossible in valid data) degrades to a best-effort
    per-node verdict via the seed-as-own-source fallback, never a crash."""
    nodes = {n.id: n for n in graph.nodes}
    vcones: dict[str, dict[str, set[str]]] = {}
    in_cones: dict[str, dict[str, set[str]]] = {}
    for nid in _toposort([n.id for n in graph.nodes], deps):
        base = _merge_consumed(nodes[nid].input_surface, vcones)
        in_cones[nid] = base
        for a in nodes[nid].output_surface:
            vc = {aid: set(vers) for aid, vers in base.items()}
            vc[a.artifact_id] = {a.artifact_version_id}   # subsumes prior of its own artifact
            vcones[a.artifact_version_id] = vc
    return vcones, in_cones


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
    # VERSION_MIX: an artifact reaching the node at >1 live version in its lineage. Two distinct
    # version ids of one artifact ARE the mix (linear revisions collapsed by subsumption), so
    # we do not gate on version_number: a pair carrying NULL numbers is still flagged.
    mixed = []
    for aid, vids in in_cone.items():
        if len(vids) < 2:
            continue
        refs = sorted((ref_of[v] for v in vids if v in ref_of), key=lambda r: r.artifact_version_id)
        if len(refs) < 2:  # need >=2 resolvable versions to name the mix
            continue
        lead = refs[0]  # deterministic pick (min version id), not set-iteration order
        nums = tuple(sorted({r.version_number for r in refs if r.version_number is not None}))
        mixed.append(VersionIssue(lead.filename, aid, nums, lead.latest_version_number))
    mixed = tuple(sorted(mixed, key=lambda m: m.artifact))
    level = VERSION_MIX if mixed else STALE_INPUT if stale else CLEAN
    return Verdict(level=level, stale=stale, mixed=mixed)


def audit_graph(graph: Graph) -> dict[str, Verdict]:
    """Per-node verdict for the whole graph, in one topological pass. Sources are always CLEAN."""
    producer_of = _producer_map(graph)
    ref_of = _ref_map(graph)
    deps = _deps(graph, producer_of)
    _vcones, in_cones = _cones(graph, deps)
    return {n.id: _verdict(n, in_cones[n.id], ref_of) for n in graph.nodes}


def issues(items) -> list:
    """A VersionIssue tuple -> JSON-safe dicts (filename + version numbers in play). Shared by
    audit_project / audit_input_lineage and review_kit so the 'issue' shape is defined once."""
    return [{"artifact": i.artifact, "versions": list(i.versions), "current": i.current}
            for i in items]


def flagged_verdicts(verdicts: dict, label: dict) -> list:
    """The non-CLEAN verdicts as sorted flag dicts ``{cell, verdict, stale, mixed}`` — one
    serializer shared by audit_project and review_kit, so the conflicts shape lives in one place."""
    return sorted(
        ({"cell": label.get(nid, nid), "verdict": v.level,
          "stale": issues(v.stale), "mixed": issues(v.mixed)}
         for nid, v in verdicts.items() if v.level != CLEAN),
        key=lambda r: r["cell"],
    )


def graph_response(graph: Graph) -> dict:
    """The ``getGraph`` wire shape both surfaces serve: ``asdict(graph)`` + each node's ``verdict``.
    The ONE authoritative serializer — server + in-CS kernel both call it, so the JSON the cockpit
    consumes can't drift between them."""
    resp = asdict(graph)
    verdicts = audit_graph(graph)
    for nd in resp["nodes"]:
        nd["verdict"] = asdict(verdicts[nd["id"]])
    return resp


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
    vcones, _in_cones = _cones(graph, deps)
    planned = Node(
        id="\x00planned", cs_project_id=graph.cs_project_id, kind="computation", label="planned",
        input_surface=tuple(ref_of[v] for v in input_version_ids),
    )
    return _verdict(planned, _merge_consumed(planned.input_surface, vcones), ref_of)
