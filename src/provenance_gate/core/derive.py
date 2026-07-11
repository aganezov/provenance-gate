"""Pure derivation: neutral records (plain dicts) → the immutable node/edge/frame Graph.

The deployment-agnostic heart. It never touches a DB or a host API — an adapter fetches the rows
(raw sqlite, or CS ``host.query``) and hands them here as dicts; the same Graph comes out. Keeping
this reader-agnostic is what stops "write once, debug twice": both surfaces derive identically as
long as they supply the same records.

Record contract each GraphReader adapter must satisfy (dict keys):
  version : id, artifact_id, version_number, checksum, storage_path, parent_version_id,
            producing_cell_id, frame_id, filename, latest_version_id (the artifact's authoritative
            head — trusted even when its row is outside the fetched set, e.g. a subgraph cone),
            latest_version_number (the head's version_number, via the reader's head-join —
            REQUIRED for correct off-cone currency; a head with no resolvable number, an in-set
            cross-artifact pointer, or no pointer at all falls back to max version_number in-set)
  dep     : consumer_v, input_v, reference_name
  cell    : id, frame_id, cell_index, source
  frame   : id, task_summary, name, parent_frame_id
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Optional

from .model import ArtifactRef, Edge, Frame, Graph, Node


def _latest_by_artifact(versions: dict[str, dict]) -> dict[str, tuple]:
    """Per artifact_id, its current head as ``(latest_version_id, latest_version_number)``.

    Trust the ``artifacts.latest_version_id`` pointer each row carries (CS advances it on a re-run,
    repoints it on a rollback). Crucially the pointer is authoritative EVEN when the head version's
    own row is outside the fetched set — normal for a subgraph cone (the newer version lives on a
    branch the cone omits). Trusting it there keeps staleness certain: an off-cone head means every
    in-cone version is correctly non-current, where a ``max(version_number)`` fallback
    would falsely crown the highest in-cone version (a false CLEAN).

    An in-set head of the SAME artifact is always trusted (its number may be NULL). An off-set head
    is trusted only when the head-join resolved a number; because that join is constrained to
    ``head.artifact_id = a.id``, a resolved number means a real same-artifact head exists off-cone.
    We fall back to ``max(version_number)`` in-set when the head is UNRESOLVABLE: no pointer; an
    in-set pointer to a DIFFERENT artifact (bad FK); or an off-set pointer with no resolved number
    (dangling, or cross-artifact filtered by the join). Trusting a dangling/foreign head would turn
    corruption into a certain verdict, so we don't. The scan is by version id and ties break on the
    higher id, so a disagreeing head pointer resolves deterministically across derives."""
    head: dict[str, tuple] = {}   # artifact_id -> (latest_version_id, latest_version_number)
    # deterministic scan order: a disagreeing head pointer (corruption/concurrency across chunked
    # fetches) must resolve the same across derives, not "whichever row was fetched first".
    for v in sorted(versions.values(), key=lambda r: r["id"]):
        aid = v["artifact_id"]
        if aid in head:
            continue
        ptr = v.get("latest_version_id")
        if not ptr:
            continue
        in_set = versions.get(ptr)
        if in_set is not None:
            if in_set["artifact_id"] == aid:                 # resolvable in-set head, same artifact
                head[aid] = (ptr, in_set["version_number"])  # trust it (its own number may be NULL)
            # else: an in-set head of a DIFFERENT artifact is a bad FK — reject, fall to max.
            # Skipping the off-set num check is deliberate: any latest_version_number here is either
            # NULL (the reader's head-join is constrained to head.artifact_id = a.id) or a foreign
            # number we would not trust anyway — nothing safe to read.
            continue
        # off-set head: trust ONLY when the head-join resolved a number (a real same-artifact head
        # row exists; the join is constrained to head.artifact_id = a.id). No number => dangling or
        # cross-artifact: unresolvable, so fall through to max.
        num = v.get("latest_version_number")
        if num is not None:
            head[aid] = (ptr, num)
    fallback: dict[str, dict] = {}
    for v in versions.values():
        aid = v["artifact_id"]
        if aid in head:
            continue
        cur = fallback.get(aid)
        v_num = v["version_number"] or 0
        cur_num = (cur["version_number"] or 0) if cur is not None else -1
        if cur is None or v_num > cur_num or (v_num == cur_num and v["id"] > cur["id"]):
            fallback[aid] = v
    for aid, v in fallback.items():
        head[aid] = (v["id"], v["version_number"])
    return head


def _ref(v: dict, latest: tuple) -> ArtifactRef:
    latest_id, latest_num = latest   # the artifact's current head: (version_id, number|None)
    return ArtifactRef(
        artifact_version_id=v["id"],
        artifact_id=v["artifact_id"],
        version_number=v["version_number"],
        filename=v["filename"],
        checksum=v["checksum"],
        storage_path=v["storage_path"],
        parent_version_id=v["parent_version_id"],
        is_latest=v["id"] == latest_id,
        latest_version_id=latest_id,
        latest_version_number=latest_num,
    )


def _producer_id(v: dict) -> str:
    """The node that produced this version: its producing cell, or a source node for an upload."""
    return v["producing_cell_id"] or f"source:{v['id']}"


def _build_nodes(
    project_id: str,
    versions: dict[str, dict],
    cells: dict[str, dict],
    deps: list[dict],
    latest: dict[str, tuple],
) -> list[Node]:
    """Group each version under its producing node, then build source + computation Nodes."""
    outputs_by_node: dict[str, list[dict]] = defaultdict(list)
    for v in versions.values():
        outputs_by_node[_producer_id(v)].append(v)
    inputs_by_node: dict[str, set[str]] = defaultdict(set)
    for d in deps:
        cv = versions.get(d["consumer_v"])
        if cv is not None:
            inputs_by_node[_producer_id(cv)].add(d["input_v"])

    def ref(v: dict) -> ArtifactRef:  # tag each ref with its artifact's current version
        return _ref(v, latest[v["artifact_id"]])

    nodes: list[Node] = []
    for nid, out_versions in outputs_by_node.items():
        out_surface = tuple(ref(v) for v in sorted(out_versions, key=lambda r: r["id"]))
        if nid.startswith("source:"):
            nodes.append(
                Node(
                    id=nid,
                    cs_project_id=project_id,
                    kind="source",
                    label=out_versions[0]["filename"] or f"source {out_versions[0]['id'][:8]}",
                    output_surface=out_surface,
                    cs_frame_id=out_versions[0]["frame_id"],
                )
            )
            continue
        cell = cells.get(nid)
        # the frame carries the task message now — a cell node is just "cell N"
        label = f"cell {cell['cell_index']}" if cell and cell["cell_index"] is not None else nid
        # filter to known version ids BEFORE sorting (a NULL input_v would break sorted())
        ivs = sorted(iv for iv in inputs_by_node.get(nid, ()) if iv in versions)
        in_refs = tuple(ref(versions[iv]) for iv in ivs)
        nodes.append(
            Node(
                id=nid,
                cs_project_id=project_id,
                kind="computation",
                label=label,
                input_surface=in_refs,
                output_surface=out_surface,
                cs_frame_id=cell["frame_id"] if cell else None,
                cs_cell_id=nid,
                cell_index=cell["cell_index"] if cell else None,
                code=cell["source"] if cell else None,
            )
        )
    return nodes


def _build_edges(deps: list[dict], versions: dict[str, dict]) -> list[Edge]:
    """Consumer-consumes-producer edges, deduped, skipping intra-node (same-cell) dependencies."""
    edges: list[Edge] = []
    seen: set[str] = set()
    for d in deps:
        cv, iv = versions.get(d["consumer_v"]), versions.get(d["input_v"])
        if cv is None or iv is None:
            continue
        src, dst = _producer_id(iv), _producer_id(cv)
        if src == dst:
            continue  # intra-node dependency (same cell) — not a graph edge
        eid = f"{src}->{dst}:{d['input_v']}"
        if eid in seen:
            continue
        seen.add(eid)
        edges.append(
            Edge(
                id=eid,
                src_node_id=src,
                dst_node_id=dst,
                via_artifact_version_id=d["input_v"],
                reference_name=d["reference_name"],
            )
        )
    return edges


def _build_frames(frames_raw: list[dict], keep_ids: set[str]) -> tuple[Frame, ...]:
    """One Frame per referenced id, titled by ``task_summary`` (else name); structural, no trust.
    ``keep_ids`` = frame ids a node references, so an over-fetch stays exact."""
    frames = [
        Frame(
            id=f["id"],
            label=(f["task_summary"] or f["name"] or f"frame {f['id'][:8]}"),
            parent_frame_id=f["parent_frame_id"],
        )
        for f in frames_raw
        if f["id"] in keep_ids
    ]
    return tuple(sorted(frames, key=lambda fr: fr.id))


def empty_graph(project_id: str, built_at: Optional[float] = None) -> Graph:
    """A project with no artifact versions — still gets a real ``built_at`` snapshot token."""
    return Graph(
        cs_project_id=project_id,
        built_at=built_at if built_at is not None else time.time(),
    )


def derive_graph(
    project_id: str,
    versions: dict[str, dict],
    deps: list[dict],
    cells: dict[str, dict],
    frames_raw: list[dict],
    built_at: Optional[float] = None,
) -> Graph:
    """Neutral records → the immutable Graph. Pure and deterministic given ``built_at``."""
    if not versions:
        return empty_graph(project_id, built_at)
    latest = _latest_by_artifact(versions)
    nodes = sorted(_build_nodes(project_id, versions, cells, deps, latest), key=lambda n: n.id)
    keep = {n.cs_frame_id for n in nodes if n.cs_frame_id}
    frames = _build_frames(frames_raw, keep)
    edges = tuple(sorted(_build_edges(deps, versions), key=lambda e: e.id))
    return Graph(
        cs_project_id=project_id,
        nodes=tuple(nodes),
        edges=edges,
        frames=frames,
        built_at=built_at if built_at is not None else time.time(),
    )
