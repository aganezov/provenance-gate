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

from ...core import derive, walk
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


def _chunked_in(conn: sqlite3.Connection, sql: str, ids, prefix_params=()) -> list[sqlite3.Row]:
    """Run ``sql`` (one ``{q}`` IN placeholder) over ids in batches, concatenating rows. Keeps each
    statement under SQLITE_MAX_VARIABLE_NUMBER so large projects still derive. ``prefix_params`` are
    bound before each chunk's ids (e.g. a project_id scoping the IN list)."""
    ids = tuple(ids)
    rows: list[sqlite3.Row] = []
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        stmt = sql.format(q=",".join("?" * len(chunk)))
        rows.extend(conn.execute(stmt, (*prefix_params, *chunk)).fetchall())
    return rows


# Shared version projection. Every row carries the artifact's authoritative head id AND its number
# (via the head-join), so core.derive judges currency straight off the row — correct even when the
# head version's OWN row is outside the fetched set (a subgraph cone: the newer version lives on a
# branch the cone doesn't include). The full read and the seeded cone read differ only in the WHERE.
_VERSION_SELECT = (
    "SELECT av.id, av.artifact_id, av.version_number, av.checksum, av.storage_path, "
    "av.parent_version_id, av.producing_cell_id, av.frame_id, a.filename, "
    "a.latest_version_id, head.version_number AS latest_version_number "
    "FROM artifact_versions av "
    "JOIN artifacts a ON a.id = av.artifact_id "
    # head-join constrained to the SAME artifact: a cross-artifact/cross-project head FK resolves to
    # NULL, so core.derive treats it as unresolvable and falls to max — no foreign number leaks in.
    "LEFT JOIN artifact_versions head ON head.id = a.latest_version_id AND head.artifact_id = a.id"
)


def fetch_versions(conn: sqlite3.Connection, project_id: str) -> dict[str, dict]:
    """Every artifact version in the project as plain dicts, keyed by version id."""
    rows = conn.execute(_VERSION_SELECT + " WHERE a.project_id = ?", (project_id,))
    return {r["id"]: dict(r) for r in rows}


def fetch_versions_by_ids(conn: sqlite3.Connection, project_id: str, vids) -> dict[str, dict]:
    """A specific set of versions (a cone closure) as plain dicts, keyed by version id. Scoped to
    ``project_id`` so a cone that closes over a cross-project dependency edge can't leak foreign
    artifacts into a graph labelled for this project (the full read is project-scoped too)."""
    rows = _chunked_in(
        conn, _VERSION_SELECT + " WHERE a.project_id = ? AND av.id IN ({q})", vids, (project_id,)
    )
    return {r["id"]: dict(r) for r in rows}


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


def _derive_from_versions(
    conn: sqlite3.Connection, project_id: str, versions: dict[str, dict]
) -> Graph:
    """Shared tail: given the project's fetched version records, fetch their deps/cells/frames and
    derive the immutable Graph (pure derive in ``core.derive``). The full-project read and a seeded
    cone read differ ONLY in which versions they fetch; everything from here down is identical.

    Frames are fetched for every version/cell frame id (a superset of what nodes reference); the
    derive keeps only the referenced ones, so the output matches a node-scoped fetch exactly.
    """
    if not versions:
        return derive.empty_graph(project_id)
    deps = fetch_deps(conn, tuple(versions))
    cells = fetch_cells(conn, versions)
    frame_ids = {v["frame_id"] for v in versions.values() if v["frame_id"]}
    frame_ids |= {c["frame_id"] for c in cells.values() if c["frame_id"]}
    frames_raw = fetch_frames(conn, frame_ids)
    return derive.derive_graph(project_id, versions, deps, cells, frames_raw)


def _expand_up(conn: sqlite3.Connection, project_id: str):
    """An upstream expander for ``core.walk.closure``, SCOPED to the project: a frontier of version
    ids -> the version ids they directly depend on whose version is in THIS project. Scoping the
    walk (not only the fetch) stops a cross-project dependency edge from bridging the walk back into
    an unrelated in-project version reachable only through a foreign one."""

    def expand(frontier) -> set:
        rows = _chunked_in(
            conn,
            "SELECT DISTINCT d.depends_on_version_id AS v FROM artifact_dependencies d "
            "JOIN artifact_versions dv ON dv.id = d.depends_on_version_id "
            "JOIN artifacts da ON da.id = dv.artifact_id "
            "WHERE da.project_id = ? AND d.depends_on_version_id IS NOT NULL "
            "AND d.artifact_version_id IN ({q})",
            tuple(frontier),
            (project_id,),
        )
        return {r["v"] for r in rows}

    return expand


def _scoped_seeds(conn: sqlite3.Connection, project_id: str, seeds) -> set:
    """The subset of ``seeds`` (version ids) that belong to ``project_id`` — so a foreign or unknown
    seed can't START a walk that pulls this project's versions in through it. With the edge-scoped
    walk, validating the seeds keeps the whole closure provably in-project (unknown -> empty)."""
    rows = _chunked_in(
        conn,
        "SELECT av.id FROM artifact_versions av JOIN artifacts a ON a.id = av.artifact_id "
        "WHERE a.project_id = ? AND av.id IN ({q})",
        tuple(seeds),
        (project_id,),
    )
    return {r["id"] for r in rows}


def read_graph(conn: sqlite3.Connection, project_id: str, *, seeds=None) -> Graph:
    """Derive a Graph for ``project_id``. ``seeds=None`` reads the WHOLE project (identical to the
    old ``read_project_graph``). Given ``seeds`` (an iterable of version ids), reads their FULL
    UPSTREAM cone: every version the seeds transitively depend on, walked to a fixpoint.

    The cone is verdict-complete for its own nodes — each node's full upstream is in the set — and
    staleness stays certain even for an artifact whose newer head lives off-cone (``core.derive``
    trusts the resolvable head pointer). There is deliberately no depth bound: a truncated cone
    drops boundary nodes' inputs, which reads as a false CLEAN — bounded/downstream walks land with
    explicit boundary marking so no node is ever asserted CLEAN on incomplete lineage.
    """
    if seeds is None:
        versions = fetch_versions(conn, project_id)
    else:
        ids = walk.closure(_scoped_seeds(conn, project_id, seeds), _expand_up(conn, project_id))
        versions = fetch_versions_by_ids(conn, project_id, ids)
    return _derive_from_versions(conn, project_id, versions)


def read_project_graph(conn: sqlite3.Connection, project_id: str) -> Graph:
    """The whole project's Graph — the no-seed case of ``read_graph``."""
    return read_graph(conn, project_id)


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

    def read_graph(self, project_id: str, *, seeds=None) -> Graph:
        """The whole project (``seeds=None``) or the upstream cone of ``seeds`` (version ids)."""
        conn = open_cs_db(self.db_path)
        try:
            return read_graph(conn, project_id, seeds=seeds)
        finally:
            conn.close()
