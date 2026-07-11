"""Tests for the independent structural oracle against synthetic operon fixtures.

Encodes the shapes the oracle must judge — a version-mix merge and a currency (stale) chain — and
the clean controls that must NOT be flagged: a linear revision and a same-version merge. The operon
fixture builder is shared with the checkpoint tests (``operon_fixture``).
"""

from __future__ import annotations

import sqlite3

from claude_science_rollouts.oracle import open_readonly, snapshot_operon, upstream_closure
from operon_fixture import Operon, diamond


def test_linear_revision_is_clean():
    """cells.qc.csv re-versioned in place (v1 -> v2), downstream reads v2: no mix, no stale."""
    op = Operon()
    cells = op.artifact("cells.csv")
    qc = op.artifact("cells.qc.csv")
    summary = op.artifact("summary.csv")
    cv = op.version(cells, 1, latest=True)
    op.version(qc, 1, reads=[cv])                       # v1, superseded, orphan leaf
    qc2 = op.version(qc, 2, reads=[cv], latest=True)    # v2, current
    op.version(summary, 1, reads=[qc2], latest=True)    # reads the current version
    v = op.verdict()
    assert v.inconsistent is False
    assert v.mixed == ()
    assert v.stale == ()


def test_version_mix_diamond_is_flagged():
    """Two branches pin different qc versions and merge: version mix at the merge node."""
    op = Operon()
    qc1, qc2, merge = diamond(op)
    v = op.verdict()
    assert v.inconsistent is True
    assert len(v.mixed) == 1
    mix = v.mixed[0]
    assert (mix.artifact, mix.versions, mix.merge_node) == ("cells.qc.csv", (1, 2), merge)
    assert mix.version_ids == (qc1, qc2)   # the exact pins the content cross-check consumes
    # Branch A rests on a superseded qc -> also a currency finding.
    assert any(s.artifact == "cells.qc.csv" and s.pinned == 1 and s.latest == 2 for s in v.stale)


def test_leaf_not_hidden_by_cross_project_consumer():
    """A consumer in another project must not hide this project's terminal node."""
    op = Operon()
    _, _, merge = diamond(op)
    op.conn.execute("INSERT INTO projects VALUES(?,?)", ("proj_other", "other"))
    op.conn.execute(
        "INSERT INTO artifacts(id, project_id, filename, latest_version_id) VALUES(?,?,?,NULL)",
        ("a_other", "proj_other", "downstream.csv"),
    )
    op.conn.execute(
        "INSERT INTO artifact_versions(id, artifact_id, version_number, checksum) VALUES(?,?,?,?)",
        ("v_other", "a_other", 1, "sha_other"),
    )
    op.conn.execute(
        "INSERT INTO artifact_dependencies VALUES(?,?,?,?)", ("d_cross", "v_other", merge, None)
    )
    v = op.verdict()
    assert v.inconsistent is True
    assert v.mixed[0].merge_node == merge


def test_same_version_merge_is_clean_control():
    """Both branches pin the SAME qc version and merge: no mix (the false-positive guard)."""
    op = Operon()
    cells = op.artifact("cells.csv")
    qc = op.artifact("cells.qc.csv")
    comp = op.artifact("composition.csv")
    sig = op.artifact("signature.csv")
    merged = op.artifact("combined_report.csv")
    cv = op.version(cells, 1, latest=True)
    qc1 = op.version(qc, 1, reads=[cv], latest=True)
    comp1 = op.version(comp, 1, reads=[qc1], latest=True)
    sig1 = op.version(sig, 1, reads=[qc1], latest=True)
    op.version(merged, 1, reads=[comp1, sig1], latest=True)
    v = op.verdict()
    assert v.inconsistent is False
    assert v.mixed == ()
    assert v.stale == ()


def test_currency_without_merge():
    """A downstream left on a superseded version, with no merge: stale but not inconsistent."""
    op = Operon()
    cells = op.artifact("cells.csv")
    qc = op.artifact("cells.qc.csv")
    summary = op.artifact("summary.csv")
    cv = op.version(cells, 1, latest=True)
    qc1 = op.version(qc, 1, reads=[cv])
    op.version(qc, 2, reads=[cv], latest=True)          # re-run supersedes v1
    op.version(summary, 1, reads=[qc1], latest=True)    # but summary still reads v1
    v = op.verdict()
    assert v.inconsistent is False
    assert v.mixed == ()
    assert len(v.stale) == 1
    assert (v.stale[0].artifact, v.stale[0].pinned, v.stale[0].latest) == ("cells.qc.csv", 1, 2)


def test_upstream_closure_follows_dependencies_not_revisions():
    """The closure reaches inputs through consumption edges only; a revision link is not a path."""
    op = Operon()
    cells = op.artifact("cells.csv")
    qc = op.artifact("cells.qc.csv")
    cv = op.version(cells, 1, latest=True)
    op.version(qc, 1, reads=[cv])
    qc2 = op.version(qc, 2, reads=[cv], latest=True)   # v2 revises v1 but depends on cells, not v1
    assert upstream_closure(op.conn, op.pid, qc2) == {qc2, cv}   # qc v1 is NOT reachable


def test_snapshot_roundtrip(tmp_path):
    """snapshot_operon copies bytes into a run dir and the frozen copy opens read-only intact."""
    src = tmp_path / "src.db"
    seed = sqlite3.connect(src)
    seed.execute("CREATE TABLE t(x)")
    seed.execute("INSERT INTO t VALUES(1)")
    seed.commit()
    seed.close()
    snap = snapshot_operon(src, tmp_path / "run")
    conn = open_readonly(snap)
    try:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        conn.close()
