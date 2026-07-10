"""External GraphReader adapter — reads Claude Science's operon DB read-only and hands the core
neutral records to derive the graph. We never write CS.

The pure derive lives in ``core.derive``; this module only *fetches* (SQL) and adapts rows to the
plain-dict record contract the core expects, so the in-CS skill's ``host.query`` reader can reuse
the identical derive. The ``fetch_*`` / ``read_project_graph`` functions take a live
``sqlite3.Connection`` (tests inject an in-memory fixture); ``CsDbReader`` is the path-based
``core.ports.GraphReader`` the external server/activation wire in.
"""

from __future__ import annotations

import sqlite3

from ...core import derive
from ...core.model import Graph


def open_cs_db(path: str) -> sqlite3.Connection:
    """Open the operon DB strictly read-only — CS is the source of truth; we never mutate it."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_projects(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Projects available in the store, newest first (for the UI's project picker)."""
    rows = conn.execute("SELECT id, name FROM projects ORDER BY updated_at DESC").fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


def _chunked_in(conn: sqlite3.Connection, sql: str, ids) -> list[sqlite3.Row]:
    """Run ``sql`` (one ``{q}`` IN placeholder) over ids in batches, concatenating rows. Keeps each
    statement under SQLITE_MAX_VARIABLE_NUMBER so large projects still derive."""
    ids = tuple(ids)
    rows: list[sqlite3.Row] = []
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        rows.extend(conn.execute(sql.format(q=",".join("?" * len(chunk))), chunk).fetchall())
    return rows


def fetch_versions(conn: sqlite3.Connection, project_id: str) -> dict[str, dict]:
    """Artifact versions in the project as plain dicts, keyed by version id (filename joined)."""
    return {
        r["id"]: dict(r)
        for r in conn.execute(
            """
            SELECT av.id, av.artifact_id, av.version_number, av.checksum, av.storage_path,
                   av.parent_version_id, av.producing_cell_id, av.frame_id, a.filename,
                   a.latest_version_id
            FROM artifact_versions av
            JOIN artifacts a ON a.id = av.artifact_id
            WHERE a.project_id = ?
            """,
            (project_id,),
        )
    }


def fetch_deps(conn: sqlite3.Connection, vids: tuple[str, ...]) -> list[dict]:
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


def fetch_cells(conn: sqlite3.Connection, versions: dict[str, dict]) -> dict[str, dict]:
    """The producing cells (frame/cell_index/code) as plain dicts, keyed by cell id."""
    cell_ids = {v["producing_cell_id"] for v in versions.values() if v["producing_cell_id"]}
    rows = _chunked_in(
        conn,
        "SELECT id, frame_id, cell_index, source FROM execution_log WHERE id IN ({q})",
        cell_ids,
    )
    return {r["id"]: dict(r) for r in rows}


def fetch_frames(conn: sqlite3.Connection, frame_ids: set[str]) -> list[dict]:
    """Raw frame rows (id/task_summary/name/parent) as plain dicts, for the referenced ids."""
    rows = _chunked_in(
        conn,
        "SELECT id, task_summary, name, parent_frame_id FROM frames WHERE id IN ({q})",
        frame_ids,
    )
    return [dict(r) for r in rows]


def read_project_graph(conn: sqlite3.Connection, project_id: str) -> Graph:
    """Fetch this project's rows and derive its immutable Graph (pure derive in ``core.derive``).

    Frames are fetched for every version/cell frame id (a superset of what nodes reference); the
    derive keeps only the referenced ones, so the output matches a node-scoped fetch exactly.
    """
    versions = fetch_versions(conn, project_id)
    if not versions:
        return derive.empty_graph(project_id)
    deps = fetch_deps(conn, tuple(versions))
    cells = fetch_cells(conn, versions)
    frame_ids = {v["frame_id"] for v in versions.values() if v["frame_id"]}
    frame_ids |= {c["frame_id"] for c in cells.values() if c["frame_id"]}
    frames_raw = fetch_frames(conn, frame_ids)
    return derive.derive_graph(project_id, versions, deps, cells, frames_raw)


class CsDbReader:
    """Path-based ``core.ports.GraphReader``: opens the operon DB read-only per call and derives.
    This is what the external server/activation wire in; the conn-based functions above stay for
    tests and the fixtures script (they inject an in-memory connection)."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def list_projects(self) -> list[dict[str, str]]:
        conn = open_cs_db(self.db_path)
        try:
            return list_projects(conn)
        finally:
            conn.close()

    def read_project_graph(self, project_id: str) -> Graph:
        conn = open_cs_db(self.db_path)
        try:
            return read_project_graph(conn, project_id)
        finally:
            conn.close()
