"""In-CS GraphReader — reads the project graph via the CS host's ``host.query`` and hands the core
the *same* neutral records the external raw-DB reader does, so both surfaces derive one Graph.

The CS kernel sandbox can't touch the operon DB directly (raw sqlite is blocked); reads go through
``host.query(sql, scope="project")``. This adapter issues the same four fetches ``substrate`` does
(versions / deps / cells / frames), shapes the rows into ``core.derive``'s record contract, and
calls the identical derive — the convergence point with the CS ``merge-lineage-audit`` skill.

``CsHost`` is the minimal host surface we need; a real kernel adapts ``host.query`` (normalising its
row shape) into it, tests pass a fake backed by sqlite. Grounded in the live merge-lineage-audit
skill (host.query proven). Scoping is a constructor choice: external/tests filter by ``project_id``;
in CS pass ``scope_by_host=True`` to rely on host.query's ``scope="project"`` (how the proven
merge-lineage-audit skill isolates a project). Large ``IN`` lists aren't chunked yet (TODO).
"""

from __future__ import annotations

from typing import Protocol

from ...core.derive import derive_graph, empty_graph
from ...core.model import Graph


class CsHost(Protocol):
    """The minimal Claude Science host surface this reader needs: a project-scoped SQL query that
    returns rows as plain dicts. A real kernel adapts ``host.query(sql, scope="project")`` (which
    may hand back ``{rows, columns}`` or dict-rows) into this shape; tests back it with sqlite."""

    def query(self, sql: str) -> list[dict]: ...


def _esc(v: object) -> str:
    """Single-quote escape for inline SQL (host.query takes a string; no bound params)."""
    return str(v).replace("'", "''")


def _in(ids) -> str:
    """A quoted, comma-joined ``IN`` list (``''`` when empty so the SQL stays valid)."""
    ids = tuple(ids)
    return ",".join("'" + _esc(i) + "'" for i in ids) if ids else "''"


# The skill's OWN render outputs (render_cockpit writes + save_artifacts these). Excluded from every
# graph so the cockpit shows the user's science, not its own plumbing. Kept in sync with what
# render_cockpit saves by a drift test (test_render_outputs_are_all_excluded).
SELF_ARTIFACTS = ("cockpit.html", "cytoscape-dagre.bundle.min.js")


class HostQueryReader:
    """The in-CS skill's GraphReader: read via ``host.query`` and derive with ``core.derive`` — the
    same Graph the external ``CsDbReader`` produces from the same project."""

    def __init__(self, host: CsHost, *, scope_by_host: bool = False):
        # scope_by_host=True: rely on the CS host's scope="project" and emit no project_id filter
        # (the in-CS reality); False: filter by project_id (external multi-project DB / tests).
        self.host = host
        self.scope_by_host = scope_by_host

    def read_project_graph(self, project_id: str) -> Graph:
        # Two independent clauses: project scoping (external only; in-CS host.query auto-scopes) and
        # the always-on self-artifact exclusion. The exclusion is NULL-safe: a NULL filename must be
        # KEPT, not dropped (in SQL, ``NULL NOT IN (...)`` is NULL, i.e. false).
        clauses = []
        if not self.scope_by_host:
            clauses.append(f"a.project_id = '{_esc(project_id)}'")
        clauses.append(f"(a.filename IS NULL OR a.filename NOT IN ({_in(SELF_ARTIFACTS)}))")
        where = " WHERE " + " AND ".join(clauses)
        versions = {
            r["id"]: r
            for r in self.host.query(
                # mirrors substrate.py's fetch_* (kept in sync by hand; the kernel reader must be
                # import-free / self-contained for inlining)
                "SELECT av.id, av.artifact_id, av.version_number, av.checksum, av.storage_path, "
                "av.parent_version_id, av.producing_cell_id, av.frame_id, a.filename "
                "FROM artifact_versions av JOIN artifacts a ON a.id = av.artifact_id" + where
            )
        }
        if not versions:
            return empty_graph(project_id)
        deps = self.host.query(
            "SELECT artifact_version_id AS consumer_v, depends_on_version_id AS input_v, "
            "reference_name FROM artifact_dependencies "
            f"WHERE artifact_version_id IN ({_in(versions)})"
        )
        deps.sort(key=lambda d: (d["consumer_v"] or "", d["input_v"] or "",
                                 d["reference_name"] or ""))
        cell_ids = {v["producing_cell_id"] for v in versions.values() if v["producing_cell_id"]}
        cells = {
            r["id"]: r
            for r in self.host.query(
                "SELECT id, frame_id, cell_index, source FROM execution_log "
                f"WHERE id IN ({_in(cell_ids)})"
            )
        }
        frame_ids = {v["frame_id"] for v in versions.values() if v["frame_id"]}
        frame_ids |= {c["frame_id"] for c in cells.values() if c["frame_id"]}
        frames = self.host.query(
            "SELECT id, task_summary, name, parent_frame_id FROM frames "
            f"WHERE id IN ({_in(frame_ids)})"
        )
        return derive_graph(project_id, versions, deps, cells, frames)
