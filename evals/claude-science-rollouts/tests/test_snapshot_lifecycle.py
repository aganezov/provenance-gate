"""Lifecycle tests for rotating snapshots and the persistence barrier."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from claude_science_rollouts.persistence.snapshots import (
    SnapshotBarrierConfig,
    SnapshotStabilityTimeout,
    await_stable_project_snapshot,
    cleanup_snapshot_path,
    observe_project,
    observe_project_settled,
)
from operon_fixture import SCHEMA


def _seed_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO projects VALUES(?, ?)", ("project-1", "test"))
    conn.commit()
    conn.close()


def _snapshot_files(run_dir: Path) -> list[Path]:
    return list((run_dir / "snapshots").rglob("*.db"))


def test_promotes_only_second_stable_observation_and_rotates(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    run_dir = tmp_path / "run"
    _seed_database(source)
    observations = iter(("one", "two", "three", "three"))
    concurrent_counts: list[int] = []

    def observer(_conn: sqlite3.Connection, _project_id: str) -> object:
        polling = run_dir / "snapshots" / ".polling"
        concurrent_counts.append(len(list(polling.glob("session-*/poll-*"))))
        return next(observations)

    async def no_wait(_delay: float) -> None:
        return None

    result = asyncio.run(
        await_stable_project_snapshot(
            source,
            "project-1",
            run_dir,
            observer=observer,
            sleep=no_wait,
        )
    )

    assert result.attempts == 4
    assert result.observation == "three"
    assert result.path == run_dir / "snapshots" / "final" / "live.db"
    assert result.path.exists()
    assert max(concurrent_counts) == 2
    assert _snapshot_files(run_dir) == [result.path]
    assert not (run_dir / "snapshots" / ".polling").exists()


def test_default_observer_stabilizes_real_project_rows(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    run_dir = tmp_path / "run"
    _seed_database(source)

    async def no_wait(_delay: float) -> None:
        return None

    def observer(conn: sqlite3.Connection, project_id: str) -> object:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE write_probe(x)")
        return observe_project(conn, project_id)

    result = asyncio.run(
        await_stable_project_snapshot(
            source,
            "project-1",
            run_dir,
            observer=observer,
            sleep=no_wait,
        )
    )

    assert result.attempts == 2
    assert result.observation.project == ("project-1", "test")
    assert result.path.exists()


@dataclass(frozen=True)
class _ReadyObservation:
    ready: bool
    value: str


def test_barrier_does_not_settle_on_a_stable_not_ready_observation(tmp_path: Path) -> None:
    # two equal not-ready observations must not be promoted to stable; the barrier waits for a ready
    # observation that repeats, rather than settling on a still-writing turn.
    source = tmp_path / "live.db"
    run_dir = tmp_path / "run"
    _seed_database(source)
    observations = iter(
        (
            _ReadyObservation(False, "pending"),
            _ReadyObservation(False, "pending"),
            _ReadyObservation(True, "done"),
            _ReadyObservation(True, "done"),
        )
    )

    async def no_wait(_delay: float) -> None:
        return None

    result = asyncio.run(
        await_stable_project_snapshot(
            source,
            "project-1",
            run_dir,
            observer=lambda _conn, _project_id: next(observations),
            sleep=no_wait,
        )
    )

    assert result.attempts == 4
    assert result.observation == _ReadyObservation(True, "done")


# operon-shaped schema WITH the dependency_mappings column, so observe_project_settled's readiness
# logic is exercised (the artifact-only fixture SCHEMA has no such column).
_SETTLING_SCHEMA = """
CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE artifacts(
    id TEXT PRIMARY KEY, project_id TEXT, filename TEXT, latest_version_id TEXT);
CREATE TABLE artifact_versions(
    id TEXT PRIMARY KEY, artifact_id TEXT, version_number INTEGER, parent_version_id TEXT,
    checksum TEXT, dependency_mappings TEXT, producing_cell_id TEXT);
CREATE TABLE artifact_dependencies(
    id TEXT PRIMARY KEY, artifact_version_id TEXT, depends_on_version_id TEXT, reference_name TEXT);
"""


def _mappings(*version_ids: str) -> str:
    return json.dumps({"inputs": [{"filename": vid, "version_id": vid} for vid in version_ids]})


def _settling_conn() -> sqlite3.Connection:
    # two uploads (no producing cell, no declared inputs) plus a computed output (a producing cell)
    # declaring both as inputs.
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SETTLING_SCHEMA)
    conn.execute("INSERT INTO projects VALUES('p', 'test')")
    for aid, filename, vid in (
        ("a_cells", "cells.csv", "v_cells"),
        ("a_params", "qc_params.csv", "v_params"),
        ("a_qc", "cells.qc.csv", "v_qc"),
    ):
        conn.execute("INSERT INTO artifacts VALUES(?, 'p', ?, ?)", (aid, filename, vid))
    conn.execute("INSERT INTO artifact_versions VALUES('v_cells','a_cells',1,NULL,'c',NULL,NULL)")
    conn.execute("INSERT INTO artifact_versions VALUES('v_params','a_params',1,NULL,'c',NULL,NULL)")
    conn.execute(
        "INSERT INTO artifact_versions VALUES('v_qc','a_qc',1,NULL,'c',?,'cell_qc')",
        (_mappings("v_cells", "v_params"),),
    )
    conn.commit()
    return conn


def test_settled_observer_waits_until_declared_edges_land() -> None:
    conn = _settling_conn()
    # v_qc declares two inputs but no edges exist yet -> not settled.
    assert observe_project_settled(conn, "p").ready is False
    conn.execute("INSERT INTO artifact_dependencies VALUES('d1', 'v_qc', 'v_cells', 'cells.csv')")
    conn.commit()
    # one of the two declared inputs has an edge -> still not settled.
    assert observe_project_settled(conn, "p").ready is False
    conn.execute("INSERT INTO artifact_dependencies VALUES('d2', 'v_qc', 'v_params', 'qc.csv')")
    conn.commit()
    # both declared inputs now have edges -> settled.
    assert observe_project_settled(conn, "p").ready is True


def test_settled_observer_ignores_inputs_without_a_resolved_version_id() -> None:
    conn = _settling_conn()
    # an input CS hasn't resolved to a version can't be waited on, so it must not block the barrier.
    conn.execute(
        "UPDATE artifact_versions SET dependency_mappings = ? WHERE id = 'v_qc'",
        (json.dumps({"inputs": [{"filename": "x.csv", "version_id": None}]}),),
    )
    conn.commit()
    assert observe_project_settled(conn, "p").ready is True


def test_settled_observer_ready_without_dependency_mappings_column() -> None:
    # the artifact-only fixtures have no dependency_mappings column: the observer must fall back to
    # pure structural stability (vacuously ready), never error, so those operons still snapshot.
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO projects VALUES('project-1', 'test')")
    conn.execute("INSERT INTO artifacts VALUES('a', 'project-1', 'f.csv', 'v1')")
    conn.execute("INSERT INTO artifact_versions VALUES('v1', 'a', 1, NULL, 'c')")
    conn.commit()
    observation = observe_project_settled(conn, "project-1")
    assert observation.ready is True
    assert observation.base.project == ("project-1", "test")


def test_settled_observer_counts_edges_and_does_not_stall_on_id_mismatch() -> None:
    # readiness counts landed edges rather than matching specific ids, so an edge CS wrote to a
    # different-but-valid version can't hang the barrier to a timeout: two edges satisfy two
    # declared inputs even though v_params was not the recorded target of the second one.
    conn = _settling_conn()
    conn.execute("INSERT INTO artifact_dependencies VALUES('d1', 'v_qc', 'v_cells', 'cells.csv')")
    conn.execute("INSERT INTO artifact_dependencies VALUES('d2', 'v_qc', 'v_other', 'other.csv')")
    conn.commit()
    assert observe_project_settled(conn, "p").ready is True


def test_settled_observer_waits_for_a_computed_declaration_to_land() -> None:
    # a computed version (one with a producing cell) whose dependency_mappings has not been written
    # yet counts as not-settled, so a snapshot caught before the declaration lands is not complete.
    conn = _settling_conn()
    conn.execute("UPDATE artifact_versions SET dependency_mappings = NULL WHERE id = 'v_qc'")
    conn.commit()
    assert observe_project_settled(conn, "p").ready is False
    conn.execute(
        "UPDATE artifact_versions SET dependency_mappings = ? WHERE id = 'v_qc'",
        (_mappings("v_cells", "v_params"),),
    )
    conn.execute("INSERT INTO artifact_dependencies VALUES('d1', 'v_qc', 'v_cells', 'cells.csv')")
    conn.execute("INSERT INTO artifact_dependencies VALUES('d2', 'v_qc', 'v_params', 'qc.csv')")
    conn.commit()
    assert observe_project_settled(conn, "p").ready is True


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


def test_barrier_returns_a_settled_pair_that_crosses_the_deadline(tmp_path: Path) -> None:
    # a settled pair found on a poll that crosses the deadline is still a good snapshot; it must be
    # returned, not discarded as a timeout on the very poll that succeeds.
    source = tmp_path / "live.db"
    run_dir = tmp_path / "run"
    _seed_database(source)
    clock = _Clock()
    calls = iter(range(100))

    def observer(_conn: sqlite3.Connection, _project_id: str) -> object:
        if next(calls) == 1:  # the second capture crosses the deadline
            clock.now += 0.2
        return _ReadyObservation(True, "settled")

    result = asyncio.run(
        await_stable_project_snapshot(
            source,
            "project-1",
            run_dir,
            config=SnapshotBarrierConfig(poll_interval_seconds=0.1, timeout_seconds=0.15),
            observer=observer,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
    )
    assert result.attempts == 2
    assert result.observation == _ReadyObservation(True, "settled")


def test_timeout_cleans_polling_snapshots_by_default(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    run_dir = tmp_path / "run"
    _seed_database(source)
    clock = _Clock()
    counter = iter(range(100))

    with pytest.raises(SnapshotStabilityTimeout):
        asyncio.run(
            await_stable_project_snapshot(
                source,
                "project-1",
                run_dir,
                config=SnapshotBarrierConfig(
                    poll_interval_seconds=0.1,
                    timeout_seconds=0.25,
                ),
                observer=lambda _conn, _project_id: next(counter),
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )
        )

    assert _snapshot_files(run_dir) == []
    assert not (run_dir / "snapshots" / ".polling").exists()


def test_failed_snapshot_retention_is_explicit_and_bounded(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    run_dir = tmp_path / "run"
    _seed_database(source)
    clock = _Clock()
    counter = iter(range(100))

    def observer(_conn: sqlite3.Connection, _project_id: str) -> object:
        observation = next(counter)
        if observation == 1:
            clock.now += 0.2
        return observation

    with pytest.raises(SnapshotStabilityTimeout):
        asyncio.run(
            await_stable_project_snapshot(
                source,
                "project-1",
                run_dir,
                config=SnapshotBarrierConfig(
                    poll_interval_seconds=0.1,
                    timeout_seconds=0.25,
                    retain_failed_snapshots=True,
                ),
                observer=observer,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )
        )

    retained = _snapshot_files(run_dir)
    assert len(retained) == 1
    assert all("failed" in path.parts for path in retained)
    assert retained[0].parent.name == "poll-000002"
    assert not (run_dir / "snapshots" / ".polling").exists()


def test_exception_cleans_polling_snapshots(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    run_dir = tmp_path / "run"
    _seed_database(source)

    def observer(_conn: sqlite3.Connection, _project_id: str) -> object:
        raise RuntimeError("observer failed")

    with pytest.raises(RuntimeError, match="observer failed"):
        asyncio.run(
            await_stable_project_snapshot(
                source,
                "project-1",
                run_dir,
                observer=observer,
            )
        )

    assert _snapshot_files(run_dir) == []
    assert not (run_dir / "snapshots" / ".polling").exists()


def test_cancellation_during_poll_wait_cleans_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    run_dir = tmp_path / "run"
    _seed_database(source)

    async def cancel(_delay: float) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            await_stable_project_snapshot(
                source,
                "project-1",
                run_dir,
                sleep=cancel,
            )
        )

    assert _snapshot_files(run_dir) == []
    assert not (run_dir / "snapshots" / ".polling").exists()


def test_cleanup_rejects_root_and_outside_paths(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    inside = root / "session" / "snapshot.db"
    outside = tmp_path / "outside.db"
    inside.parent.mkdir(parents=True)
    inside.touch()
    outside.touch()

    with pytest.raises(ValueError):
        cleanup_snapshot_path(root, root=root)
    with pytest.raises(ValueError):
        cleanup_snapshot_path(outside, root=root)

    assert inside.exists()
    assert outside.exists()
    cleanup_snapshot_path(inside.parent, root=root)
    assert not inside.exists()


def test_cleanup_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "escaped"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        cleanup_snapshot_path(link, root=root)

    assert link.is_symlink()
    assert outside.exists()


def test_snapshot_root_cannot_be_symlinked_outside_run(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    run_dir = tmp_path / "run"
    outside = tmp_path / "outside"
    _seed_database(source)
    run_dir.mkdir()
    outside.mkdir()
    (run_dir / "snapshots").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="snapshot root cannot be a symlink"):
        asyncio.run(await_stable_project_snapshot(source, "project-1", run_dir))

    assert list(outside.iterdir()) == []


def test_polling_directory_cannot_be_symlinked_outside_root(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    run_dir = tmp_path / "run"
    snapshot_root = run_dir / "snapshots"
    outside = tmp_path / "outside"
    _seed_database(source)
    snapshot_root.mkdir(parents=True)
    outside.mkdir()
    (snapshot_root / ".polling").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="snapshot directory cannot be a symlink"):
        asyncio.run(await_stable_project_snapshot(source, "project-1", run_dir))

    assert list(outside.iterdir()) == []
