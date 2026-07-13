"""Offline coverage for the single-project snapshot copier.

Builds a synthetic operon (the version-mix diamond) with a second project sharing the file, freezes
one project's rows, and checks that the copy holds exactly that project's evidence, stays
referentially closed, and fingerprints deterministically.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_science_rollouts.persistence.project_snapshot import (
    ProjectSnapshotError,
    _select_in,
    materialize_project_snapshot,
)
from operon_fixture import Operon, diamond


def _two_project_operon(path: Path) -> Path:
    """Write a file operon holding project ``proj_a`` (the diamond) plus a foreign ``proj_b``."""
    op = Operon(project_id="proj_a")
    diamond(op)
    op.conn.commit()

    dest = sqlite3.connect(path)
    op.conn.backup(dest)
    op.conn.close()

    # proj_b's one dependency edge reaches back into a proj_a version on purpose: the copier must
    # exclude rows by ownership, not by reachability, so this foreign row has to stay out.
    borrowed = dest.execute("SELECT id FROM artifact_versions LIMIT 1").fetchone()[0]
    dest.execute("INSERT INTO projects VALUES (?, ?)", ("proj_b", "other"))
    dest.execute(
        "INSERT INTO artifacts VALUES (?, ?, ?, ?)", ("b_art", "proj_b", "other.csv", "vb1")
    )
    dest.execute(
        "INSERT INTO artifact_versions VALUES (?, ?, ?, ?, ?)",
        ("vb1", "b_art", 1, None, "b" * 64),
    )
    dest.execute(
        "INSERT INTO artifact_dependencies VALUES (?, ?, ?, ?)",
        ("db1", "vb1", borrowed, "cross"),
    )
    dest.commit()
    dest.close()
    return path


def _readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def test_snapshot_holds_only_the_target_project(tmp_path: Path) -> None:
    source = _two_project_operon(tmp_path / "operon.db")
    snapshot = materialize_project_snapshot(source, tmp_path / "proj_a.db", "proj_a")

    conn = _readonly(snapshot.path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT id FROM projects").fetchall() == [("proj_a",)]
        # every copied artifact belongs to proj_a, and the copy is non-empty.
        assert conn.execute("SELECT DISTINCT project_id FROM artifacts").fetchall() == [("proj_a",)]
        assert conn.execute("SELECT count(*) FROM artifact_versions").fetchone()[0] > 0
    finally:
        conn.close()

    assert len(snapshot.sha256) == 64
    assert snapshot.size_bytes == snapshot.path.stat().st_size


def test_foreign_project_rows_are_excluded(tmp_path: Path) -> None:
    source = _two_project_operon(tmp_path / "operon.db")
    snapshot = materialize_project_snapshot(source, tmp_path / "proj_a.db", "proj_a")

    conn = _readonly(snapshot.path)
    try:
        assert conn.execute("SELECT id FROM projects WHERE id = 'proj_b'").fetchall() == []
        assert conn.execute("SELECT id FROM artifacts WHERE id = 'b_art'").fetchall() == []
        assert conn.execute("SELECT id FROM artifact_versions WHERE id = 'vb1'").fetchall() == []
        # the foreign edge points into proj_a's versions but is owned by proj_b, so it is dropped.
        edges = conn.execute("SELECT id FROM artifact_dependencies WHERE id = 'db1'").fetchall()
        assert edges == []
    finally:
        conn.close()


def test_snapshot_is_referentially_closed(tmp_path: Path) -> None:
    source = _two_project_operon(tmp_path / "operon.db")
    snapshot = materialize_project_snapshot(source, tmp_path / "proj_a.db", "proj_a")

    conn = _readonly(snapshot.path)
    try:
        orphan_versions = conn.execute(
            "SELECT count(*) FROM artifact_versions v "
            "WHERE v.artifact_id NOT IN (SELECT id FROM artifacts)"
        ).fetchone()[0]
        assert orphan_versions == 0
        # both endpoints of every retained dependency edge resolve inside the copy: the diamond is
        # self-contained, so neither the owning version nor its upstream target dangles.
        dangling_edges = conn.execute(
            "SELECT count(*) FROM artifact_dependencies d "
            "WHERE d.artifact_version_id NOT IN (SELECT id FROM artifact_versions) "
            "   OR d.depends_on_version_id NOT IN (SELECT id FROM artifact_versions)"
        ).fetchone()[0]
        assert dangling_edges == 0
    finally:
        conn.close()


def test_sha256_is_deterministic(tmp_path: Path) -> None:
    source = _two_project_operon(tmp_path / "operon.db")
    first = materialize_project_snapshot(source, tmp_path / "first.db", "proj_a")
    second = materialize_project_snapshot(source, tmp_path / "second.db", "proj_a")
    assert first.sha256 == second.sha256
    assert first.size_bytes == second.size_bytes


def test_unknown_project_is_rejected(tmp_path: Path) -> None:
    source = _two_project_operon(tmp_path / "operon.db")
    with pytest.raises(ProjectSnapshotError, match="exactly one project row"):
        materialize_project_snapshot(source, tmp_path / "missing.db", "proj_missing")


def test_select_in_chunks_under_the_sqlite_variable_limit() -> None:
    # a project with more ids than SQLite's variable cap (999 on older builds) must still copy; the
    # IN clause is chunked, so a 1500-id read stays under a cap we pin low here to prove it.
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(id TEXT PRIMARY KEY, k TEXT)")
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    ids = [f"id-{i:04d}" for i in range(1500)]
    conn.executemany("INSERT INTO t VALUES(?, 'x')", [(value,) for value in ids])
    rows = _select_in(conn, "t", "id", ids)
    assert len(rows) == 1500
    assert {row[0] for row in rows} == set(ids)
