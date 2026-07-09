"""Append-only per-project event log — a parseable timeline of what the gate did, both sides.

One ``events.jsonl`` per project workspace (``<state-dir>/<pid>/events.jsonl``); global /
pre-activation events go to ``<state-dir>/session.jsonl``. Every line is self-describing and
stamped with a ``run_id`` (this process's, or the UI's own) and, for request-driven events, a
``req_id`` that correlates a UI action with the server work it triggered. Ground truth for
debugging — not a replay/command log.

Line: ``{ts, run_id, req_id, project_id, src, kind, ms?, ...}``
(``src`` = server | ui;  ``kind`` = http | op | fetch | action | error.)
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
import uuid

from . import workspace

#: minted once per server process; UI events carry their own per-page-load run_id
SERVER_RUN_ID = uuid.uuid4().hex[:8]

_lock = threading.Lock()  # ThreadingHTTPServer handler threads all append; serialize writes


def _events_path(state_dir: str, project_id: str | None) -> pathlib.Path:
    """The log file an event belongs in: the project's, or the global session log."""
    if project_id and workspace.is_valid_pid(project_id):
        return workspace.project_dir(state_dir, project_id, create=True) / "events.jsonl"
    return pathlib.Path(state_dir) / "session.jsonl"


def _append(path: pathlib.Path, line: dict) -> None:
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(line, separators=(",", ":")) + "\n")


def emit(
    state_dir: str,
    *,
    src: str,
    kind: str,
    level: str = "info",
    project_id: str | None = None,
    req_id: str | None = None,
    run_id: str | None = None,
    ms: float | None = None,
    **extra: object,
) -> None:
    """Append one server-side event to the right log file (the project's, or session's)."""
    line: dict = {
        "ts": round(time.time(), 3),
        "run_id": run_id or SERVER_RUN_ID,
        "req_id": req_id,
        "project_id": project_id,
        "src": src,
        "kind": kind,
        "level": level,
    }
    if ms is not None:
        line["ms"] = round(ms, 1)
    line.update(extra)
    _append(_events_path(state_dir, project_id), line)


def write_client_event(state_dir: str, ev: dict) -> None:
    """Append a client event (from ``POST /api/log``), routed by its validated project_id."""
    if not isinstance(ev, dict):
        return
    pid = ev.get("project_id")
    ev["src"] = "ui"  # unconditional — a client POST cannot forge a server-looking src
    ev.setdefault("level", "info")
    ev["ts_recv"] = round(time.time(), 3)  # server clock, for cross-side ordering
    _append(_events_path(state_dir, pid if isinstance(pid, str) else None), ev)
