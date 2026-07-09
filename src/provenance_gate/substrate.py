"""Read-only reader over Claude Science's operon DB → the immutable node/edge graph.

We never write CS. We open the DB read-only and derive, for one project:
  - **node** = a producing cell (a *computation*); or a *source* node for an
    upload/external input that no cell produced.
  - **edge** = "consumer cell consumes an artifact produced by producer cell",
    read straight from ``artifact_dependencies``.
  - **frame** = the CS task a cell belongs to (``frames.task_summary``); nodes link via
    ``cs_frame_id`` and the UI draws frames as bounding containers. Structural only — no trust.

Only the handful of columns we need are read; the rest of the large operon schema
is ignored. ``read_project_graph`` takes a live ``sqlite3.Connection`` so tests can
inject an in-memory fixture; production opens the real DB via ``open_cs_db`` (``mode=ro``).
"""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict

from .model import ArtifactRef, Edge, Frame, Graph, Node


def open_cs_db(path: str) -> sqlite3.Connection:
    """Open the operon DB strictly read-only — CS is the source of truth; we never mutate it."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_projects(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Projects available in the store, newest first (for the UI's project picker)."""
    rows = conn.execute(
        "SELECT id, name FROM projects ORDER BY updated_at DESC"
    ).fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


def _ref(v: sqlite3.Row) -> ArtifactRef:
    return ArtifactRef(
        artifact_version_id=v["id"],
        artifact_id=v["artifact_id"],
        version_number=v["version_number"],
        filename=v["filename"],
        checksum=v["checksum"],
        storage_path=v["storage_path"],
        parent_version_id=v["parent_version_id"],
    )


def _producer_id(v: sqlite3.Row) -> str:
    """The node that produced this version: its producing cell, or a source node for an upload."""
    return v["producing_cell_id"] or f"source:{v['id']}"


def _load_versions(conn: sqlite3.Connection, project_id: str) -> dict[str, sqlite3.Row]:
    """All artifact versions in the project, keyed by version id (filename joined in)."""
    return {
        r["id"]: r
        for r in conn.execute(
            """
            SELECT av.id, av.artifact_id, av.version_number, av.checksum, av.storage_path,
                   av.parent_version_id, av.producing_cell_id, av.frame_id, a.filename
            FROM artifact_versions av
            JOIN artifacts a ON a.id = av.artifact_id
            WHERE a.project_id = ?
            """,
            (project_id,),
        )
    }


def _chunked_in(conn: sqlite3.Connection, sql: str, ids) -> list[sqlite3.Row]:
    """Run ``sql`` (one ``{q}`` IN placeholder) over ids in batches, concatenating rows. Keeps each
    statement under SQLITE_MAX_VARIABLE_NUMBER so large projects still derive."""
    ids = tuple(ids)
    rows: list[sqlite3.Row] = []
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        rows.extend(conn.execute(sql.format(q=",".join("?" * len(chunk))), chunk).fetchall())
    return rows


def _load_deps(conn: sqlite3.Connection, vids: tuple[str, ...]) -> list[dict]:
    """artifact_dependencies among these versions, globally sorted so edge dedup (keep-first of a
    collided id) stays stable across derives (no ORDER-BY churn)."""
    rows = _chunked_in(
        conn,
        "SELECT artifact_version_id AS consumer_v, depends_on_version_id AS input_v, "
        "reference_name FROM artifact_dependencies WHERE artifact_version_id IN ({q})",
        vids,
    )
    deps = [dict(r) for r in rows]
    deps.sort(key=lambda d: (d["consumer_v"] or "", d["input_v"] or "", d["reference_name"] or ""))
    return deps


def _load_cells(
    conn: sqlite3.Connection, versions: dict[str, sqlite3.Row]
) -> dict[str, sqlite3.Row]:
    """The producing cells (frame/cell_index/code), keyed by cell id."""
    cell_ids = {v["producing_cell_id"] for v in versions.values() if v["producing_cell_id"]}
    rows = _chunked_in(
        conn,
        "SELECT id, frame_id, cell_index, source FROM execution_log WHERE id IN ({q})",
        cell_ids,
    )
    return {r["id"]: r for r in rows}


def _load_frames(conn: sqlite3.Connection, frame_ids: set[str]) -> list[Frame]:
    """One Frame per referenced id, titled by ``task_summary`` (else name). No trust."""
    rows = _chunked_in(
        conn,
        "SELECT id, task_summary, name, parent_frame_id FROM frames WHERE id IN ({q})",
        frame_ids,
    )
    return [
        Frame(
            id=r["id"],
            label=(r["task_summary"] or r["name"] or f"frame {r['id'][:8]}"),
            parent_frame_id=r["parent_frame_id"],
        )
        for r in rows
    ]


def _build_nodes(
    project_id: str,
    versions: dict[str, sqlite3.Row],
    cells: dict[str, sqlite3.Row],
    deps: list[dict],
) -> list[Node]:
    """Group each version under its producing node, then build source + computation Nodes."""
    outputs_by_node: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for v in versions.values():
        outputs_by_node[_producer_id(v)].append(v)
    inputs_by_node: dict[str, set[str]] = defaultdict(set)
    for d in deps:
        cv = versions.get(d["consumer_v"])
        if cv is not None:
            inputs_by_node[_producer_id(cv)].add(d["input_v"])

    nodes: list[Node] = []
    for nid, out_versions in outputs_by_node.items():
        out_surface = tuple(_ref(v) for v in sorted(out_versions, key=lambda r: r["id"]))
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
        in_refs = tuple(_ref(versions[iv]) for iv in ivs)
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


def _build_edges(deps: list[dict], versions: dict[str, sqlite3.Row]) -> list[Edge]:
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


def read_project_graph(conn: sqlite3.Connection, project_id: str) -> Graph:
    """Derive the immutable node/edge Graph for one CS project. Pure read; no writes."""
    versions = _load_versions(conn, project_id)
    if not versions:
        return Graph(cs_project_id=project_id, built_at=time.time())
    deps = _load_deps(conn, tuple(versions))
    cells = _load_cells(conn, versions)
    nodes = sorted(_build_nodes(project_id, versions, cells, deps), key=lambda n: n.id)
    frame_ids = {n.cs_frame_id for n in nodes if n.cs_frame_id}
    frames = tuple(sorted(_load_frames(conn, frame_ids), key=lambda f: f.id))
    edges = sorted(_build_edges(deps, versions), key=lambda e: e.id)
    return Graph(
        cs_project_id=project_id, nodes=tuple(nodes), edges=tuple(edges),
        frames=frames, built_at=time.time(),
    )
