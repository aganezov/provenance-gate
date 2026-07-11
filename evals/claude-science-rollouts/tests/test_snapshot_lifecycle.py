"""Lifecycle tests for rotating snapshots and the persistence barrier."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from claude_science_rollouts.persistence.snapshots import (
    SnapshotBarrierConfig,
    SnapshotStabilityTimeout,
    await_stable_project_snapshot,
    cleanup_snapshot_path,
    observe_project,
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


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


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
