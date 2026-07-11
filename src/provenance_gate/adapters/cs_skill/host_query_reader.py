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
from ...core.walk import closure


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
SELF_ARTIFACTS = ("cockpit.html", "cytoscape-dagre.bundle.min.js", "cockpit-app.js")


class HostQueryReader:
    """The in-CS skill's GraphReader: read via ``host.query`` and derive with ``core.derive`` — the
    same Graph the external ``CsDbReader`` produces from the same project."""

    def __init__(self, host: CsHost, *, scope_by_host: bool = False):
        # scope_by_host=True: rely on the CS host's scope="project" and emit no project_id filter
        # (the in-CS reality); False: filter by project_id (external multi-project DB / tests).
        self.host = host
        self.scope_by_host = scope_by_host

    def _version_where(self, project_id: str, *, extra: str = "") -> str:
        # Two independent clauses: project scoping (external only; in-CS host.query auto-scopes) and
        # the always-on self-artifact exclusion. The exclusion is NULL-safe: a NULL filename must be
        # KEPT, not dropped (``NULL NOT IN (...)`` is NULL, false). ``extra`` narrows to a cone.
        clauses = []
        if not self.scope_by_host:
            clauses.append(f"a.project_id = '{_esc(project_id)}'")
        clauses.append(f"(a.filename IS NULL OR a.filename NOT IN ({_in(SELF_ARTIFACTS)}))")
        if extra:
            clauses.append(extra)
        return " WHERE " + " AND ".join(clauses)

    def _chunked(self, ids, build_sql) -> list:
        # host.query analogue of substrate._chunked_in: run ``build_sql(quoted_in_list)`` over ids
        # in batches, concatenating rows, so a large cone can't blow the inline-SQL statement size.
        ids = tuple(ids)
        if not ids:
            return []
        rows = []
        for i in range(0, len(ids), 900):
            rows.extend(self.host.query(build_sql(_in(ids[i:i + 900]))))
        return rows

    def _version_sql(self, where: str) -> str:
        # mirrors substrate.py's projection (kept in sync by hand; the kernel reader must be self-
        # contained for inlining). The head-join carries the artifact's current version NUMBER (not
        # just id), so core.derive judges currency off the row — correct even off a cone. It is
        # constrained to the SAME artifact so a cross-artifact/cross-project head FK resolves to
        # NULL (core.derive then falls back to max — no foreign number leaks in).
        return (
            "SELECT av.id, av.artifact_id, av.version_number, av.checksum, av.storage_path, "
            "av.parent_version_id, av.producing_cell_id, av.frame_id, a.filename, "
            "a.latest_version_id, head.version_number AS latest_version_number "
            "FROM artifact_versions av JOIN artifacts a ON a.id = av.artifact_id "
            "LEFT JOIN artifact_versions head "
            "ON head.id = a.latest_version_id AND head.artifact_id = a.id" + where
        )

    def _fetch_versions(self, where: str) -> dict:
        # the whole-project read (no id IN-list to chunk — bounded only by project + self-exclusion)
        return {r["id"]: r for r in self.host.query(self._version_sql(where))}

    def _expand_up(self, project_id: str):
        # a project-SCOPED upstream expander for core.walk.closure: one hop up, keeping only
        # dependencies whose version is in this project — so a cross-project edge can't bridge the
        # walk back to an unrelated in-project version (host.query also scopes; matches substrate).
        scope = "" if self.scope_by_host else f"da.project_id = '{_esc(project_id)}' AND "

        def expand(frontier) -> set:
            rows = self._chunked(
                frontier,
                lambda inl: (
                    "SELECT DISTINCT d.depends_on_version_id AS v FROM artifact_dependencies d "
                    "JOIN artifact_versions dv ON dv.id = d.depends_on_version_id "
                    "JOIN artifacts da ON da.id = dv.artifact_id "
                    f"WHERE {scope}d.depends_on_version_id IS NOT NULL "
                    f"AND d.artifact_version_id IN ({inl})"
                ),
            )
            return {r["v"] for r in rows}

        return expand

    def _derive_from_versions(self, project_id: str, versions: dict) -> Graph:
        # shared tail: given the fetched versions, fetch their deps/cells/frames and derive. Full
        # and seeded cone reads differ ONLY in which versions they fetch.
        if not versions:
            return empty_graph(project_id)
        deps = self._chunked(
            versions,
            lambda inl: (
                "SELECT artifact_version_id AS consumer_v, depends_on_version_id AS input_v, "
                f"reference_name FROM artifact_dependencies WHERE artifact_version_id IN ({inl})"
            ),
        )
        deps.sort(key=lambda d: (d["consumer_v"] or "", d["input_v"] or "",
                                 d["reference_name"] or ""))
        cell_ids = {v["producing_cell_id"] for v in versions.values() if v["producing_cell_id"]}
        cells = {
            r["id"]: r
            for r in self._chunked(
                cell_ids,
                lambda inl: (
                    "SELECT id, frame_id, cell_index, source FROM execution_log "
                    f"WHERE id IN ({inl})"
                ),
            )
        }
        frame_ids = {v["frame_id"] for v in versions.values() if v["frame_id"]}
        frame_ids |= {c["frame_id"] for c in cells.values() if c["frame_id"]}
        frames = self._chunked(
            frame_ids,
            lambda inl: (
                "SELECT id, task_summary, name, parent_frame_id FROM frames "
                f"WHERE id IN ({inl})"
            ),
        )
        return derive_graph(project_id, versions, deps, cells, frames)

    def _scoped_seeds(self, project_id: str, seeds) -> set:
        # the subset of seeds belonging to project_id — a foreign/unknown seed can't START a walk
        # that pulls this project's versions in through it (with the edge-scoped walk the closure
        # then stays provably in-project). scope_by_host defers to host.query's project scoping.
        seeds = tuple(seeds)
        if not seeds:
            return set()
        scope = "" if self.scope_by_host else f"a.project_id = '{_esc(project_id)}' AND "
        rows = self._chunked(
            seeds,
            lambda inl: (
                "SELECT av.id FROM artifact_versions av JOIN artifacts a ON a.id = av.artifact_id "
                f"WHERE {scope}av.id IN ({inl})"
            ),
        )
        return {r["id"] for r in rows}

    def read_graph(self, project_id: str, *, seeds=None) -> Graph:
        """The whole project (``seeds=None``) or the full UPSTREAM cone of ``seeds`` (version ids):
        every version the seeds transitively depend on, via ``core.walk.closure`` — the same cone
        the external reader produces. Currency stays certain for an off-cone head (core.derive).
        No depth bound: a truncated cone would drop boundary inputs and read as a false CLEAN."""
        if seeds is None:
            versions = self._fetch_versions(self._version_where(project_id))
        else:
            ids = closure(self._scoped_seeds(project_id, seeds), self._expand_up(project_id))
            rows = self._chunked(
                ids,
                lambda inl: self._version_sql(
                    self._version_where(project_id, extra=f"av.id IN ({inl})")
                ),
            )
            versions = {r["id"]: r for r in rows}
        return self._derive_from_versions(project_id, versions)

    def read_project_graph(self, project_id: str) -> Graph:
        """The whole project's Graph — the no-seed case of read_graph."""
        return self.read_graph(project_id)

    def resolve_seeds(self, project_id: str, refs) -> set:
        """Resolve flexible refs — filenames, artifact ids, or version ids — to the version ids they
        name (the seed frontier for read_graph). A filename or artifact id resolves to ALL of that
        artifact's versions (so focusing an artifact seeds every version — 'versions for free'); a
        version id resolves to itself. The skill's own render outputs (SELF_ARTIFACTS) are excluded
        — exactly as read_graph excludes them — so focusing one resolves to NOTHING (the caller sees
        focus_unresolved) rather than a silent empty cone. Unmatched refs contribute nothing."""
        # a str is one ref; a non-iterable scalar (int/bool — off-contract) is treated as one ref
        # too, so a stray scalar focus resolves to nothing rather than raising `for r in <scalar>`.
        seq = [refs] if isinstance(refs, str) or not hasattr(refs, "__iter__") else list(refs)
        refs = [r for r in seq if r]
        if not refs:
            return set()
        scope = "" if self.scope_by_host else f"a.project_id = '{_esc(project_id)}' AND "
        rows = self._chunked(
            refs,
            lambda inl: (
                "SELECT av.id FROM artifact_versions av JOIN artifacts a ON a.id = av.artifact_id "
                f"WHERE {scope}(a.filename IS NULL OR a.filename NOT IN ({_in(SELF_ARTIFACTS)})) "
                f"AND (a.filename IN ({inl}) OR a.id IN ({inl}) OR av.id IN ({inl}))"
            ),
        )
        return {r["id"] for r in rows}
