"""Lazy per-project activation — derive a project's graph on demand, on the read path.

Derive a project only when it's read — the cockpit polls the active one — throttled so rapid reads
serve the sidecar cache. We rewrite the sidecar and report a change only when the derived graph
actually differs. Startup is instant, only the project in view is warm, and — since the derive
happens inside the request — the log can correlate a read with the derive it triggered.
"""

from __future__ import annotations

import threading
import time

from . import substrate, workspace
from .model import Graph
from .store import Store

CACHED = "cached"        # served the sidecar without touching CS (inside the throttle window)
CHANGED = "changed"      # re-derived and the graph differed -> sidecar rewritten
UNCHANGED = "unchanged"  # re-derived but identical -> nothing written

_last_derive: dict[str, float] = {}  # pid -> last successful derive (in-memory; lost on restart)
_meta = threading.Lock()  # guards _pid_locks
_pid_locks: dict[str, threading.Lock] = {}  # per-project lock: serializes its derive+write


def _pid_lock(pid: str) -> threading.Lock:
    with _meta:
        lock = _pid_locks.get(pid)
        if lock is None:
            lock = _pid_locks[pid] = threading.Lock()
    return lock


def get_fresh(
    cs_db_path: str, state_dir: str, pid: str, min_interval: float = 1.0
) -> tuple[Graph, str]:
    """Return pid's graph, deriving from CS on demand — throttled + change-detected, serialized per
    project so overlapping requests can't race an empty cache or collide on the sidecar write."""
    store = Store(workspace.sidecar_path(state_dir, pid, create=True))
    try:
        with _pid_lock(pid):  # one derive+write per project; different projects run in parallel
            now = time.time()
            if (now - _last_derive.get(pid, 0.0)) < min_interval:
                return store.load_graph(pid), CACHED
            old = store.load_graph(pid)
            cs = substrate.open_cs_db(cs_db_path)
            try:
                new = substrate.read_project_graph(cs, pid)
            finally:
                cs.close()
            # First derive has old.built_at==0.0 (empty sidecar); store even an empty graph then,
            # so an empty project gets a real built_at (snapshot age + poll token), not epoch 0.
            changed = (
                new.nodes != old.nodes or new.edges != old.edges
                or new.frames != old.frames or old.built_at == 0.0
            )
            if changed:
                store.replace_project_graph(new)
                _last_derive[pid] = now  # advance the throttle only after a successful derive+write
                return new, CHANGED
            _last_derive[pid] = now
            return old, UNCHANGED
    finally:
        store.close()
