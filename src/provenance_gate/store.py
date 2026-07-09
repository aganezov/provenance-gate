"""Our sidecar SQLite — the DERIVED-cache storage zone.

Two-zone model (see build-philosophy):
  - **Derived cache** (this file): node/surface/edge/frame rows mirrored from CS. Rebuildable,
    so a "resync" is just ``replace_project_graph`` (delete this project's rows, re-insert).
    A schema change here = drop the file and re-derive on next read; never a migration.
  - **Owned overlay** (later): assumptions/links/acts keyed by ``node.id`` — a separate
    zone, additive, never dropped. The read layer will join the two.

This holds only the derived cache. ``surface_item`` already carries a ``role`` (input/output)
and a ``kind`` discriminator so future surface variants (e.g. linked values) are additive.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from .model import ArtifactRef, Edge, Frame, Graph, Node

SCHEMA = """
CREATE TABLE IF NOT EXISTS project_sync(cs_project_id TEXT PRIMARY KEY, built_at REAL);
CREATE TABLE IF NOT EXISTS node(
    id TEXT PRIMARY KEY, cs_project_id TEXT, kind TEXT, label TEXT,
    cs_frame_id TEXT, cs_cell_id TEXT, cell_index INTEGER, code TEXT);
CREATE TABLE IF NOT EXISTS surface_item(
    node_id TEXT, role TEXT, kind TEXT, seq INTEGER,
    artifact_version_id TEXT, artifact_id TEXT, version_number INTEGER,
    filename TEXT, checksum TEXT, storage_path TEXT, parent_version_id TEXT);
CREATE TABLE IF NOT EXISTS edge(
    id TEXT PRIMARY KEY, cs_project_id TEXT, src_node_id TEXT, dst_node_id TEXT,
    via_artifact_version_id TEXT, reference_name TEXT);
CREATE TABLE IF NOT EXISTS frame(
    id TEXT PRIMARY KEY, cs_project_id TEXT, label TEXT, parent_frame_id TEXT, kind TEXT);
CREATE INDEX IF NOT EXISTS ix_node_project ON node(cs_project_id);
CREATE INDEX IF NOT EXISTS ix_surface_node ON surface_item(node_id);
CREATE INDEX IF NOT EXISTS ix_edge_project ON edge(cs_project_id);
CREATE INDEX IF NOT EXISTS ix_frame_project ON frame(cs_project_id);
"""


def _ref(r: sqlite3.Row) -> ArtifactRef:
    return ArtifactRef(
        artifact_version_id=r["artifact_version_id"],
        artifact_id=r["artifact_id"],
        version_number=r["version_number"],
        filename=r["filename"],
        checksum=r["checksum"],
        storage_path=r["storage_path"],
        parent_version_id=r["parent_version_id"],
        kind=r["kind"],
    )


class Store:
    """Owns the sidecar DB connection. Use ``:memory:`` in tests, a file path in prod."""

    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")  # wait, not fail, on a locked sidecar
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def replace_project_graph(self, g: Graph) -> None:
        """Resync one project's derived rows: delete then re-insert (atomic)."""
        pid = g.cs_project_id
        with self.conn:  # transaction
            self.conn.execute(
                "DELETE FROM surface_item WHERE node_id IN "
                "(SELECT id FROM node WHERE cs_project_id=?)",
                (pid,),
            )
            self.conn.execute("DELETE FROM node WHERE cs_project_id=?", (pid,))
            self.conn.execute("DELETE FROM edge WHERE cs_project_id=?", (pid,))
            self.conn.execute("DELETE FROM frame WHERE cs_project_id=?", (pid,))
            self.conn.executemany(
                "INSERT INTO node VALUES(?,?,?,?,?,?,?,?)",
                [
                    (n.id, pid, n.kind, n.label, n.cs_frame_id, n.cs_cell_id, n.cell_index, n.code)
                    for n in g.nodes
                ],
            )
            surface_rows = []
            for n in g.nodes:
                for role, surface in (("input", n.input_surface), ("output", n.output_surface)):
                    for seq, a in enumerate(surface):
                        surface_rows.append((
                            n.id, role, a.kind, seq,
                            a.artifact_version_id, a.artifact_id, a.version_number,
                            a.filename, a.checksum, a.storage_path, a.parent_version_id,
                        ))
            self.conn.executemany(
                "INSERT INTO surface_item VALUES(?,?,?,?,?,?,?,?,?,?,?)", surface_rows
            )
            self.conn.executemany(
                "INSERT INTO edge VALUES(?,?,?,?,?,?)",
                [
                    (e.id, pid, e.src_node_id, e.dst_node_id,
                     e.via_artifact_version_id, e.reference_name)
                    for e in g.edges
                ],
            )
            self.conn.executemany(
                "INSERT INTO frame VALUES(?,?,?,?,?)",
                [(f.id, pid, f.label, f.parent_frame_id, f.kind) for f in g.frames],
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO project_sync VALUES(?,?)", (pid, g.built_at)
            )

    def load_graph(self, project_id: str) -> Graph:
        """Reconstruct the Graph for one project from the derived cache."""
        c = self.conn
        surfaces_in: dict[str, list[ArtifactRef]] = defaultdict(list)
        surfaces_out: dict[str, list[ArtifactRef]] = defaultdict(list)
        for r in c.execute(
            "SELECT * FROM surface_item WHERE node_id IN "
            "(SELECT id FROM node WHERE cs_project_id=?) ORDER BY node_id, role, seq",
            (project_id,),
        ):
            (surfaces_in if r["role"] == "input" else surfaces_out)[r["node_id"]].append(_ref(r))

        nodes = tuple(
            Node(
                id=r["id"],
                cs_project_id=project_id,
                kind=r["kind"],
                label=r["label"],
                input_surface=tuple(surfaces_in[r["id"]]),
                output_surface=tuple(surfaces_out[r["id"]]),
                cs_frame_id=r["cs_frame_id"],
                cs_cell_id=r["cs_cell_id"],
                cell_index=r["cell_index"],
                code=r["code"],
            )
            for r in c.execute(
                "SELECT * FROM node WHERE cs_project_id=? ORDER BY id", (project_id,)
            )
        )
        edges = tuple(
            Edge(
                id=r["id"],
                src_node_id=r["src_node_id"],
                dst_node_id=r["dst_node_id"],
                via_artifact_version_id=r["via_artifact_version_id"],
                reference_name=r["reference_name"],
            )
            for r in c.execute(
                "SELECT * FROM edge WHERE cs_project_id=? ORDER BY id", (project_id,)
            )
        )
        frames = tuple(
            Frame(
                id=r["id"], label=r["label"],
                parent_frame_id=r["parent_frame_id"], kind=r["kind"],
            )
            for r in c.execute(
                "SELECT * FROM frame WHERE cs_project_id=? ORDER BY id", (project_id,)
            )
        )
        row = c.execute(
            "SELECT built_at FROM project_sync WHERE cs_project_id=?", (project_id,)
        ).fetchone()
        return Graph(
            cs_project_id=project_id,
            nodes=nodes,
            edges=edges,
            frames=frames,
            built_at=row["built_at"] if row else 0.0,
        )
