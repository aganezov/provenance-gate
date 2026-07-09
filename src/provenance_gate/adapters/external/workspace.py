"""Per-project runtime workspace — where the gate keeps its background state, out of the repo.

The copilot is per Claude Science project, so state is laid out one directory per project:

    <state-dir>/<cs_project_id>/
        sidecar.db     # derived graph cache (+ owned overlay later)
        events.jsonl   # the log (later)
        notes/         # tester prose (later)

``<state-dir>`` is ``$PG_STATE_DIR`` if set, else ``.pg/`` under the launch directory — gitignored,
so it never pollutes the repo and is easy to inspect or wipe in dev. Set ``PG_STATE_DIR`` to
``~/.provenance-gate`` (or anywhere) to fully decouple state from the working tree.
"""

from __future__ import annotations

import os
import pathlib
import re

# a CS project id is opaque alphanumerics + _/- ; nothing that could escape the state dir.
# \A…\Z (not ^…$) so a trailing newline can't sneak through ($ matches before a final \n in Python).
_PID_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")


def is_valid_pid(pid: str) -> bool:
    """True for a safe project id. Guards the client-supplied ``?project=`` from path traversal."""
    return bool(pid) and _PID_RE.match(pid) is not None


def default_state_dir() -> pathlib.Path:
    """``$PG_STATE_DIR`` (``~`` expanded) if set, else ``.pg`` under the current directory."""
    env = os.environ.get("PG_STATE_DIR")
    return pathlib.Path(env).expanduser() if env else pathlib.Path(".pg")


def project_dir(state_dir: str | os.PathLike, pid: str, *, create: bool = False) -> pathlib.Path:
    """The per-project workspace dir; ``create=True`` makes it. Raises on an unsafe pid."""
    if not is_valid_pid(pid):
        raise ValueError(f"unsafe project id: {pid!r}")
    d = pathlib.Path(state_dir) / pid
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def sidecar_path(state_dir: str | os.PathLike, pid: str, *, create: bool = False) -> str:
    """Path to a project's derived-cache DB (``<state-dir>/<pid>/sidecar.db``)."""
    return str(project_dir(state_dir, pid, create=create) / "sidecar.db")
