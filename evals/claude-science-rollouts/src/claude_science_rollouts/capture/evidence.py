"""Compact durable evidence for one externally owned episode directory."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(slots=True)
class EpisodeEvidence:
    scenario_id: str
    project_id: str
    started_at: str = field(default_factory=utc_now)
    steps: list[dict[str, Any]] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    terminal_reason: str | None = None
    exception: dict[str, str] | None = None
    final_snapshot: dict[str, Any] | None = None

    def finish(self, terminal_reason: str) -> None:
        self.terminal_reason = terminal_reason

    def record_exception(self, exc: BaseException) -> None:
        self.exception = {
            "type": type(exc).__name__,
            "message": str(exc)[:4096],
        }

    def record_snapshot(self, snapshot_path: Path, run_dir: Path, attempts: int) -> None:
        self.final_snapshot = {
            "path": str(snapshot_path.resolve().relative_to(run_dir.resolve())),
            "sha256": file_sha256(snapshot_path),
            "stability_attempts": attempts,
        }

    def as_dict(self) -> dict[str, Any]:
        if self.terminal_reason is None:
            raise ValueError("episode evidence must have a terminal reason")
        return {
            "schema_version": 1,
            "scenario_id": self.scenario_id,
            "project_id": self.project_id,
            "started_at": self.started_at,
            "finished_at": utc_now(),
            "terminal_reason": self.terminal_reason,
            "exception": self.exception,
            "steps": self.steps,
            "turns": self.turns,
            "checkpoints": self.checkpoints,
            "final_snapshot": self.final_snapshot,
        }

    def write(self, run_dir: str | Path) -> Path:
        directory = Path(run_dir)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "episode_manifest.json"
        if target.is_symlink():
            raise ValueError("episode manifest cannot be a symlink")
        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        temporary = directory / ".episode_manifest.json.tmp"
        if temporary.is_symlink():
            raise ValueError("temporary episode manifest cannot be a symlink")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(target)
        return target
