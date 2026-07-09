"""Pure derivation: neutral records (plain dicts) → the immutable node/edge/frame Graph.

The deployment-agnostic heart. It never touches a DB or a host API — an adapter fetches the rows
(raw sqlite, or CS ``host.query``) and hands them here as dicts; the same Graph comes out. Keeping
this reader-agnostic is what stops "write once, debug twice": both surfaces derive identically as
long as they supply the same records.

Record contract each GraphReader adapter must satisfy (dict keys):
  version : id, artifact_id, version_number, checksum, storage_path, parent_version_id,
            producing_cell_id, frame_id, filename
  dep     : consumer_v, input_v, reference_name
  cell    : id, frame_id, cell_index, source
  frame   : id, task_summary, name, parent_frame_id
"""

from __future__ import annotations

import time
from collections import defaultdict

from .model import ArtifactRef, Edge, Frame, Graph, Node


def _latest_by_artifact(versions: dict[str, dict]) -> dict[str, dict]:
    """Per artifact_id, the version record with the highest ``version_number``. CS bumps it each
    re-run, so the max is the current version. (Adapters may later inject CS's authoritative
    ``artifacts.latest_version_id``; the audit/UI only need "which version is current".)
    Ties (equal or NULL ``version_number``) break on the higher version id — a stable order, so
    ``is_latest`` stays stable across derives of the same CS state."""
    latest: dict[str, dict] = {}
    for v in versions.values():
        cur = latest.get(v["artifact_id"])
        v_num = v["version_number"] or 0
        cur_num = (cur["version_number"] or 0) if cur is not None else -1
        if cur is None or v_num > cur_num or (v_num == cur_num and v["id"] > cur["id"]):
            latest[v["artifact_id"]] = v
    return latest


def _ref(v: dict, latest_v: dict) -> ArtifactRef:
    return ArtifactRef(
        artifact_version_id=v["id"],
        artifact_id=v["artifact_id"],
        version_number=v["version_number"],
        filename=v["filename"],
        checksum=v["checksum"],
        storage_path=v["storage_path"],
        parent_version_id=v["parent_version_id"],
        is_latest=v["id"] == latest_v["id"],
        latest_version_id=latest_v["id"],
        latest_version_number=latest_v["version_number"],
    )


def _producer_id(v: dict) -> str:
    """The node that produced this version: its producing cell, or a source node for an upload."""
    return v["producing_cell_id"] or f"source:{v['id']}"


def _build_nodes(
    project_id: str,
    versions: dict[str, dict],
    cells: dict[str, dict],
    deps: list[dict],
    latest: dict[str, dict],
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


def empty_graph(project_id: str, built_at: float | None = None) -> Graph:
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
    built_at: float | None = None,
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
