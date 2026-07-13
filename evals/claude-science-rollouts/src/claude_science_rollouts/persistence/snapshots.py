"""Rotating database snapshots and the project-persistence barrier."""

from __future__ import annotations

import asyncio
import json
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


@dataclass(frozen=True, slots=True)
class SettledProjectObservation:
    """A ``ProjectObservation`` plus whether the project's dependency edges have finished landing.

    CS commits an artifact version (carrying its ``dependency_mappings`` JSON of declared inputs)
    and then normalizes those inputs into ``artifact_dependencies`` edge rows asynchronously. Two
    adjacent polls can therefore agree on a *partial* edge set and look stable while edges are still
    being written. The barrier settles only on a ``ready`` observation, so it keeps polling until
    the edges catch up — the structural rows alone are not enough to call a snapshot settled.
    """

    base: ProjectObservation
    ready: bool


def _artifact_version_columns(conn: sqlite3.Connection) -> frozenset[str]:
    # the columns present on this operon's artifact_versions. The settled observer adapts to the
    # artifact-only test fixtures, which carry neither dependency_mappings nor producing_cell_id and
    # so have nothing to settle on — structural stability governs there.
    return frozenset(row[1] for row in conn.execute("PRAGMA table_info(artifact_versions)"))


def _declared_input_version_ids(mappings: object) -> set[str]:
    """The input version ids a version declares in its ``dependency_mappings`` JSON — each becomes
    one ``artifact_dependencies`` edge once CS normalizes it. Malformed or shapeless JSON, or an
    input with no resolved ``version_id``, contributes nothing, so a row we can't read never blocks
    the barrier (structural stability still governs it)."""
    if not isinstance(mappings, str):
        return set()
    try:
        data = json.loads(mappings)
    except ValueError:
        return set()
    inputs = data.get("inputs") if isinstance(data, dict) else None
    if not isinstance(inputs, list):
        return set()
    return {
        item["version_id"]
        for item in inputs
        if isinstance(item, dict) and item.get("version_id")
    }


def _edge_counts(conn: sqlite3.Connection, project_id: str) -> dict[str, int]:
    # how many artifact_dependencies edges each project version has, in one grouped query rather
    # than a query per version — this runs on every settle poll, so the fan-out matters.
    rows = conn.execute(
        "SELECT d.artifact_version_id, COUNT(*) FROM artifact_dependencies AS d "
        "JOIN artifact_versions AS v ON v.id = d.artifact_version_id "
        "JOIN artifacts AS a ON a.id = v.artifact_id "
        "WHERE a.project_id = ? GROUP BY d.artifact_version_id",
        (project_id,),
    )
    return {version_id: count for version_id, count in rows}


def _dependencies_settled(conn: sqlite3.Connection, project_id: str) -> bool:
    """True when the project's dependency edges have finished materializing.

    For every version that declares inputs, its edge COUNT must have reached the number of declared
    input version ids. Counting (rather than matching specific ids) means a legitimately-written
    edge whose target resolved to a different-but-valid version can't stall the barrier to a timeout
    — the concern is only whether the edges have landed. A COMPUTED version (one with a producing
    cell) whose ``dependency_mappings`` has not been written yet is treated as not-settled, so a
    snapshot caught before the declaration lands isn't mistaken for complete. Uploads (no declared
    inputs) and the artifact-only fixtures (which carry neither column) are vacuously settled.
    """
    columns = _artifact_version_columns(conn)
    if "dependency_mappings" not in columns:
        return True
    has_cell = "producing_cell_id" in columns
    projection = "v.id, v.dependency_mappings" + (", v.producing_cell_id" if has_cell else "")
    versions = conn.execute(
        f"SELECT {projection} FROM artifact_versions AS v "
        "JOIN artifacts AS a ON a.id = v.artifact_id WHERE a.project_id = ?",
        (project_id,),
    ).fetchall()
    edge_counts = _edge_counts(conn, project_id)
    for row in versions:
        version_id, mappings = row[0], row[1]
        producing_cell = row[2] if has_cell else None
        if producing_cell is not None and mappings is None:
            return False  # a computed output whose declared inputs have not been written yet
        required = _declared_input_version_ids(mappings)
        if required and edge_counts.get(version_id, 0) < len(required):
            return False  # declared inputs not yet fully present as edges
    return True


def observe_project_settled(
    conn: sqlite3.Connection, project_id: str
) -> SettledProjectObservation:
    """The structural project observation plus a dependency-settled readiness flag — the observer
    the episode barrier uses so a persisted snapshot always carries complete lineage, not an edge
    set CS is still writing."""
    return SettledProjectObservation(
        observe_project(conn, project_id),
        _dependencies_settled(conn, project_id),
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


def _observation_ready(observation: object) -> bool:
    # a not-ready observation (a still-writing turn) must not count as a settled outcome even when
    # two adjacent polls compare equal. an observation without a `ready` flag — project rows — is
    # always eligible, so project-snapshot stability is unchanged.
    ready = getattr(observation, "ready", True)
    if not isinstance(ready, bool):
        raise TypeError("observation readiness must be boolean")
    return ready


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
            # a settled pair wins even if this capture just crossed the deadline: the snapshot is
            # already good, so returning it beats discarding a valid result on a slow final poll.
            if (
                previous is not None
                and current.observation == previous.observation
                and _observation_ready(current.observation)
            ):
                current.directory.replace(final_dir)
                final_path = final_dir / current.path.name
                _remove_if_present(previous.directory, snapshot_root)
                completed = True
                return StableSnapshot(final_path, current.observation, attempts)
            if monotonic() >= deadline:
                raise SnapshotStabilityTimeout(attempts)

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
