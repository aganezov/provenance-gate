"""Freeze one project's provenance evidence into a standalone SQLite file.

Rollouts are graded after the fact, so the rows a grader reads must be pinned to an immutable
copy rather than the live operon database, which keeps rotating underneath. This module rebuilds
every source table's schema in a fresh file but fills only the rows that belong to a single
project id; global and cross-project tables are recreated empty. The copy is compacted,
integrity-checked, and fingerprinted so a later grader can prove it read the same bytes that were
frozen here.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from claude_science_rollouts.capture.evidence import file_sha256
from claude_science_rollouts.oracle.snapshot import open_readonly

Row = tuple[object, ...]


class ProjectSnapshotError(RuntimeError):
    """The snapshot could not be built without dropping or corrupting evidence."""


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    """A materialized single-project snapshot and its content fingerprint."""

    path: Path
    sha256: str
    size_bytes: int
    row_counts: dict[str, int]


# frame-owned tables and the column that ties each row back to a frame. transcript_annotations
# hangs off the conversation's root frame; the rest off the frame that produced the row.
_FRAME_OWNED: dict[str, str] = {
    "compaction_archives": "frame_id",
    "events": "frame_id",
    "execution_log": "frame_id",
    "frame_branch_archives": "frame_id",
    "frame_system_prompts": "frame_id",
    "transcript_annotations": "root_frame_id",
}


def _quote(identifier: str) -> str:
    # table/column names reach the DDL/DML by string interpolation, so double-quote them and
    # escape any embedded quote instead of trusting the source schema to be well-behaved.
    return '"' + identifier.replace('"', '""') + '"'


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _table_ddl(source: sqlite3.Connection) -> list[str]:
    # base tables only. indexes, views, and triggers are intentionally left behind to keep the
    # copy small, and foreign keys stay off, so the order tables are created or filled never
    # matters. sql IS NULL skips the implicit indexes sqlite records for UNIQUE/PRIMARY KEY.
    return [
        str(sql)
        for (sql,) in source.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL "
            "ORDER BY name"
        )
    ]


def _select(conn: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> list[Row]:
    # open_readonly hands back sqlite3.Row objects; flatten them to plain tuples so they bind
    # straight back as positional parameters when we re-insert into the copy.
    return [tuple(row) for row in conn.execute(sql, tuple(params))]


def _select_in(conn: sqlite3.Connection, table: str, column: str,
               values: Sequence[object]) -> list[Row]:
    if not values or not _has_table(conn, table):
        return []
    placeholders = ",".join("?" for _ in values)
    # order by rowid so the copy's row layout (and thus its fingerprint) is fixed by the source's
    # own insertion order rather than by whatever scan order sqlite happens to choose.
    return _select(
        conn,
        f"SELECT * FROM {_quote(table)} WHERE {_quote(column)} IN ({placeholders}) ORDER BY rowid",
        values,
    )


def _select_owned(conn: sqlite3.Connection, table: str, project_id: str) -> list[Row]:
    # a project-scoped table read that tolerates the table being absent, so the copier works
    # against any operon subset (the fixtures carry only the provenance-core tables).
    if not _has_table(conn, table):
        return []
    return _select(
        conn, f"SELECT * FROM {_quote(table)} WHERE project_id = ? ORDER BY id", (project_id,)
    )


def _verification_checks(conn: sqlite3.Connection, frame_ids: Sequence[object],
                         version_ids: Sequence[object]) -> list[Row]:
    # a check is in scope when it was raised on one of the project's frames, reviewed by one of
    # them, or attached to one of the project's artifact versions.
    clauses: list[str] = []
    params: list[object] = []
    for column, values in (
        ("root_frame_id", frame_ids),
        ("reviewer_frame_id", frame_ids),
        ("artifact_version_id", version_ids),
    ):
        if values:
            clauses.append(f"{_quote(column)} IN ({','.join('?' for _ in values)})")
            params.extend(values)
    if not clauses:
        return []
    return _select(
        conn,
        "SELECT * FROM verification_checks WHERE " + " OR ".join(clauses) + " ORDER BY rowid",
        params,
    )


def _collect(source: sqlite3.Connection, project_id: str) -> dict[str, list[Row]]:
    """Walk out from the project row to every table of evidence it owns."""
    if not _has_table(source, "projects"):
        raise ProjectSnapshotError("source database has no projects table")
    project = _select(source, "SELECT * FROM projects WHERE id = ?", (project_id,))
    if len(project) != 1:
        raise ProjectSnapshotError(
            f"expected exactly one project row for {project_id!r}, found {len(project)}"
        )

    artifacts = _select_owned(source, "artifacts", project_id)
    artifact_ids = [row[0] for row in artifacts]
    versions = _select_in(source, "artifact_versions", "artifact_id", artifact_ids)
    version_ids = [row[0] for row in versions]

    frames = _select_owned(source, "frames", project_id)
    frame_ids = [row[0] for row in frames]

    selected: dict[str, list[Row]] = {
        "projects": project,
        "artifacts": artifacts,
        "artifact_versions": versions,
        "artifact_dependencies": _select_in(
            source, "artifact_dependencies", "artifact_version_id", version_ids
        ),
        "frames": frames,
        "frame_messages": _select_in(source, "frame_messages", "frame_id", frame_ids),
    }
    for table, column in _FRAME_OWNED.items():
        selected[table] = _select_in(source, table, column, frame_ids)
    if _has_table(source, "annotations"):
        selected["annotations"] = _select(
            source, "SELECT * FROM annotations WHERE project_id = ?", (project_id,)
        )
    if _has_table(source, "verification_checks"):
        selected["verification_checks"] = _verification_checks(source, frame_ids, version_ids)
    return selected


def _insert(target: sqlite3.Connection, table: str, rows: Sequence[Row]) -> int:
    if not rows or not _has_table(target, table):
        return 0
    placeholders = ",".join("?" for _ in rows[0])
    target.executemany(f"INSERT INTO {_quote(table)} VALUES ({placeholders})", rows)
    return len(rows)


def _write(source: sqlite3.Connection, target: sqlite3.Connection,
           selected: dict[str, list[Row]]) -> dict[str, int]:
    target.execute("PRAGMA foreign_keys = OFF")
    target.execute("PRAGMA journal_mode = DELETE")  # one self-contained file, no -wal sidecar
    for ddl in _table_ddl(source):
        target.execute(ddl)
    # count only tables the copy actually has, so a selection key for an absent optional table
    # (empty by construction) never shows up as a phantom row_count.
    counts = {
        table: _insert(target, table, selected[table])
        for table in sorted(selected)
        if _has_table(target, table)
    }
    # carry the schema version across so the copy reads identically to the source it came from.
    (user_version,) = source.execute("PRAGMA user_version").fetchone()
    target.execute(f"PRAGMA user_version = {int(user_version)}")
    target.commit()
    target.execute("VACUUM")  # discard the free pages left by selective inserts
    result = target.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise ProjectSnapshotError(f"snapshot failed integrity check: {result!r}")
    return counts


def materialize_project_snapshot(
    source_db: str | Path, destination_db: str | Path, project_id: str
) -> ProjectSnapshot:
    """Build a standalone SQLite snapshot holding one project's evidence rows.

    ``source_db`` must already be a settled copy (see ``oracle.snapshot.snapshot_operon``); it is
    opened strictly read-only and never written back. The destination must not exist yet: the copy
    is assembled in a sibling temp file and swapped into place only once it passes its integrity
    check, so a half-written file is never observable at ``destination_db``.
    """
    if not project_id:
        raise ValueError("project_id must be non-empty")
    source_path = Path(source_db)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    destination = Path(destination_db)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")

    source = open_readonly(source_path)
    target: sqlite3.Connection | None = None
    try:
        selected = _collect(source, project_id)
        target = sqlite3.connect(scratch)
        row_counts = _write(source, target, selected)
        target.close()
        target = None
        os.replace(scratch, destination)
    except BaseException:
        if target is not None:
            target.close()
        scratch.unlink(missing_ok=True)
        raise
    finally:
        source.close()

    return ProjectSnapshot(
        path=destination,
        sha256=file_sha256(destination),
        size_bytes=destination.stat().st_size,
        row_counts=row_counts,
    )
