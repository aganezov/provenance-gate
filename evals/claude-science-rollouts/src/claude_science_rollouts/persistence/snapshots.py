"""Rotating database snapshots and the project-persistence barrier."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from claude_science_rollouts.oracle.snapshot import open_readonly, snapshot_operon

Observation = TypeVar("Observation")
Observer = Callable[[sqlite3.Connection, str], Observation]
Sleeper = Callable[[float], Awaitable[None]]


class ProjectNotFoundError(LookupError):
    """Raised when a snapshot does not contain the requested project."""


class SnapshotStabilityTimeout(TimeoutError):
    """Raised when no two adjacent project observations stabilize before the deadline."""

    def __init__(self, attempts: int) -> None:
        super().__init__(f"project did not stabilize after {attempts} snapshot attempts")
        self.attempts = attempts


@dataclass(frozen=True, slots=True)
class ProjectObservation:
    """Exact project-scoped structural rows used to detect persistence stability."""

    project: tuple[object, ...]
    artifacts: tuple[tuple[object, ...], ...]
    versions: tuple[tuple[object, ...], ...]
    dependencies: tuple[tuple[object, ...], ...]


@dataclass(frozen=True, slots=True)
class SnapshotBarrierConfig:
    """Timing and failure-retention policy for one persistence barrier."""

    poll_interval_seconds: float = 0.5
    timeout_seconds: float = 30.0
    retain_failed_snapshots: bool = False

    def __post_init__(self) -> None:
        if self.poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds cannot be negative")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class StableSnapshot(Generic[Observation]):
    """The final retained copy and the observation that crossed the barrier."""

    path: Path
    observation: Observation
    attempts: int


@dataclass(frozen=True, slots=True)
class _PollingSnapshot:
    directory: Path
    path: Path
    observation: object


def observe_project(conn: sqlite3.Connection, project_id: str) -> ProjectObservation:
    """Read all structural rows owned by ``project_id`` in deterministic order."""
    project_row = conn.execute(
        "SELECT id, name FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if project_row is None:
        raise ProjectNotFoundError(project_id)

    artifacts = conn.execute(
        """
        SELECT id, project_id, filename, latest_version_id
        FROM artifacts
        WHERE project_id = ?
        ORDER BY id
        """,
        (project_id,),
    ).fetchall()
    versions = conn.execute(
        """
        SELECT v.id, v.artifact_id, v.version_number, v.parent_version_id, v.checksum
        FROM artifact_versions AS v
        JOIN artifacts AS a ON a.id = v.artifact_id
        WHERE a.project_id = ?
        ORDER BY v.id
        """,
        (project_id,),
    ).fetchall()
    # NOTE: depends_on_version_id may reference versions in other projects, which are not fetched
    # here. Stability is complete only if CS artifact_versions rows are immutable once written
    # (content-addressed by checksum) — a new foreign target then appears as a new dependency EDGE
    # row, which is captured below.
    dependencies = conn.execute(
        """
        SELECT d.id, d.artifact_version_id, d.depends_on_version_id, d.reference_name
        FROM artifact_dependencies AS d
        JOIN artifact_versions AS v ON v.id = d.artifact_version_id
        JOIN artifacts AS a ON a.id = v.artifact_id
        WHERE a.project_id = ?
        ORDER BY d.id
        """,
        (project_id,),
    ).fetchall()
    return ProjectObservation(
        project=tuple(project_row),
        artifacts=tuple(tuple(row) for row in artifacts),
        versions=tuple(tuple(row) for row in versions),
        dependencies=tuple(tuple(row) for row in dependencies),
    )


def cleanup_snapshot_path(path: str | Path, *, root: str | Path) -> None:
    """Remove a snapshot path only when its resolved location is strictly below ``root``."""
    root_path = Path(root).resolve()
    target = Path(path)
    resolved_target = target.resolve()
    if resolved_target == root_path or not resolved_target.is_relative_to(root_path):
        raise ValueError(f"refusing to clean snapshot path outside root: {target}")
    if target.is_symlink():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()


def _remove_if_present(path: Path, snapshot_root: Path) -> None:
    if path.exists() or path.is_symlink():
        cleanup_snapshot_path(path, root=snapshot_root)


def _prepare_snapshot_root(run_dir: Path) -> Path:
    run_root = run_dir.resolve()
    snapshot_root = run_dir / "snapshots"
    if snapshot_root.is_symlink():
        raise ValueError(f"snapshot root cannot be a symlink: {snapshot_root}")
    snapshot_root.mkdir(parents=True, exist_ok=True)
    resolved_root = snapshot_root.resolve()
    if resolved_root == run_root or not resolved_root.is_relative_to(run_root):
        raise ValueError(f"snapshot root must be inside run directory: {snapshot_root}")
    return snapshot_root


def _prepare_snapshot_directory(path: Path, snapshot_root: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"snapshot directory cannot be a symlink: {path}")
    path.mkdir(exist_ok=True)
    resolved_path = path.resolve()
    resolved_root = snapshot_root.resolve()
    if resolved_path == resolved_root or not resolved_path.is_relative_to(resolved_root):
        raise ValueError(f"snapshot directory must be inside snapshot root: {path}")
    return path


def _retain_latest_poll(session_dir: Path, snapshot_root: Path) -> None:
    polling_dirs = sorted(path for path in session_dir.iterdir() if path.is_dir())
    for obsolete in polling_dirs[:-1]:
        cleanup_snapshot_path(obsolete, root=snapshot_root)


def _capture(
    src_db: Path,
    directory: Path,
    project_id: str,
    observer: Observer[Observation],
) -> _PollingSnapshot:
    path = snapshot_operon(src_db, directory)
    conn = open_readonly(path)
    try:
        observation = observer(conn, project_id)
    finally:
        conn.close()
    return _PollingSnapshot(directory, path, observation)


async def await_stable_project_snapshot(
    src_db: str | Path,
    project_id: str,
    run_dir: str | Path,
    *,
    config: SnapshotBarrierConfig | None = None,
    observer: Observer[Observation] = observe_project,
    sleep: Sleeper = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> StableSnapshot[Observation]:
    """Retain the second of two adjacent equal project snapshots as the final episode copy.

    Polling copies rotate so only the previous and current observations coexist. All transient
    paths are cleaned for success, timeout, exceptions, and cancellation. Failed copies are kept
    only when ``retain_failed_snapshots`` is explicitly enabled.
    """
    policy = config or SnapshotBarrierConfig()
    source = Path(src_db)
    snapshot_root = _prepare_snapshot_root(Path(run_dir))
    polling_root = snapshot_root / ".polling"
    final_dir = snapshot_root / "final"
    if final_dir.exists() or final_dir.is_symlink():
        raise FileExistsError(f"final snapshot already exists: {final_dir}")

    _prepare_snapshot_directory(polling_root, snapshot_root)
    session_dir = polling_root / f"session-{uuid.uuid4().hex}"
    session_dir.mkdir()

    deadline = monotonic() + policy.timeout_seconds
    previous: _PollingSnapshot | None = None
    attempts = 0
    completed = False
    try:
        while monotonic() < deadline:
            attempts += 1
            current_dir = session_dir / f"poll-{attempts:06d}"
            current = _capture(source, current_dir, project_id, observer)
            if monotonic() >= deadline:
                raise SnapshotStabilityTimeout(attempts)
            if previous is not None and current.observation == previous.observation:
                current.directory.replace(final_dir)
                final_path = final_dir / current.path.name
                _remove_if_present(previous.directory, snapshot_root)
                completed = True
                return StableSnapshot(final_path, current.observation, attempts)

            if previous is not None:
                _remove_if_present(previous.directory, snapshot_root)
            previous = current

            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            await sleep(min(policy.poll_interval_seconds, remaining))
        raise SnapshotStabilityTimeout(attempts)
    finally:
        if not completed and policy.retain_failed_snapshots and session_dir.exists():
            try:
                _retain_latest_poll(session_dir, snapshot_root)
                failed_root = _prepare_snapshot_directory(
                    snapshot_root / "failed", snapshot_root
                )
                session_dir.replace(failed_root / session_dir.name)
            except BaseException:
                _remove_if_present(session_dir, snapshot_root)
                raise
        else:
            _remove_if_present(session_dir, snapshot_root)
        if polling_root.exists() and not any(polling_root.iterdir()):
            polling_root.rmdir()
