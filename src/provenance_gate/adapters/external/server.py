"""stdlib HTTP server: thin API over service functions, serves the cockpit UI.

Topology (read path only):

    CS operon-cli.db --(read-only, on demand)--> <state-dir>/<pid>/sidecar.db (per project)
                                                          |
    cockpit.html --HTTP--> this server --thin handler--> activation --> per-project sidecar

Lazy: nothing derives at startup; ``GET /api/graph`` derives the requested project on demand
(throttled, change-detected), so only the project in view is kept warm and the derive is correlated
to its request. Every request boundary + internal op is logged to the project's ``events.jsonl``
(routine polls at ``"trace"``, changes/actions/errors at ``"info"``). The UI posts its own events
to ``POST /api/log``. Run: ``uv run pg-serve`` (external surface entrypoint).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ...core import audit
from . import activation, log, substrate, workspace

UI_DIR = Path(__file__).resolve().parents[4] / "ui"  # …/adapters/external/server.py → repo root


def default_cs_db() -> str:
    """Locate the operon DB: ``$CS_DB`` if set, else the single org under ~/.claude-science."""
    if env := os.environ.get("CS_DB"):
        return env
    hits = sorted(glob.glob(os.path.expanduser("~/.claude-science/orgs/*/operon-cli.db")))
    return hits[0] if hits else ""


def _same_version(client_v: str, built_at: float) -> bool:
    """Compare the client's echoed built_at (a JS number string) to ours numerically, so integer or
    format differences (or a stringified 0) don't defeat the unchanged short-circuit."""
    try:
        return float(client_v) == built_at
    except (TypeError, ValueError):
        return False


class Handler(BaseHTTPRequestHandler):
    # set on the class by serve() before the server starts
    cs_db_path: str = ""
    state_dir: str = ""
    min_interval: float = 1.0
    timeout: float = 30  # per-connection socket timeout so a stalled client can't pin a thread
    _status: int = 200

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self._status = code
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: object, code: int = 200) -> None:
        self._send(json.dumps(obj).encode(), "application/json", code)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        t0 = time.time()
        self._status = 200
        u = urlparse(self.path)
        req_id = self.headers.get("X-PG-Req")
        pid = parse_qs(u.query).get("project", [None])[0] if u.path == "/api/graph" else None
        try:
            self._dispatch(u, req_id)
        except Exception as e:  # noqa: BLE001 - report, never crash the handler thread
            self._status = 500
            log.emit(self.state_dir, src="server", kind="error", project_id=pid, req_id=req_id,
                     path=u.path, error=repr(e), trace=traceback.format_exc())
            try:
                self._json({"error": "internal"}, 500)
            except Exception:  # noqa: BLE001 - response may be partially sent
                pass
        finally:
            ms = (time.time() - t0) * 1000
            # the 2s poll is heartbeat -> trace; page load / project list / errors -> info
            level = "trace" if (u.path == "/api/graph" and self._status == 200) else "info"
            log.emit(self.state_dir, src="server", kind="http", level=level, project_id=pid,
                     req_id=req_id, method="GET", path=u.path, status=self._status, ms=ms)

    def _dispatch(self, u, req_id: str | None) -> None:
        if u.path in ("/", "/index.html"):
            f = UI_DIR / "cockpit.html"
            if f.exists():
                return self._send(f.read_bytes(), "text/html")
            return self._json({"error": "cockpit.html not found"}, 404)
        if not self.cs_db_path and u.path.startswith("/api/"):
            return self._json(
                {"error": "no CS database found; set CS_DB or start Claude Science first"}, 503
            )
        if u.path == "/api/projects":
            return self._json(substrate.CsDbReader(self.cs_db_path).list_projects())
        if u.path == "/api/graph":
            pid = parse_qs(u.query).get("project", [""])[0]
            if not workspace.is_valid_pid(pid):
                return self._json({"error": "invalid project id"}, 400)
            t = time.time()
            g, status = activation.get_fresh(
                self.cs_db_path, self.state_dir, pid, self.min_interval
            )
            ms = (time.time() - t) * 1000
            # only a real change is signal; a cached/unchanged re-derive is heartbeat
            level = "info" if status == activation.CHANGED else "trace"
            log.emit(self.state_dir, src="server", kind="op", level=level, name="derive",
                     project_id=pid, req_id=req_id, status=status,
                     nodes=len(g.nodes), edges=len(g.edges), ms=ms)
            # UI sends its last version (?v=built_at); unchanged -> tiny marker, UI skips re-render.
            # built_at advances only on a real change; plain 200, no cache quirks.
            if _same_version(parse_qs(u.query).get("v", [""])[0], g.built_at):
                return self._json({"unchanged": True, "built_at": g.built_at})
            # the ONE authoritative getGraph serializer (core.audit.graph_response), shared verbatim
            # with the in-CS skill kernel so the two surfaces can't drift.
            return self._json(audit.graph_response(g))
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        t0 = time.time()
        self._status = 200
        u = urlparse(self.path)
        req_id = self.headers.get("X-PG-Req")
        try:
            self._dispatch_post(u)
        except Exception as e:  # noqa: BLE001 - report, never crash the handler thread
            self._status = 500
            log.emit(self.state_dir, src="server", kind="error", req_id=req_id,
                     path=u.path, error=repr(e), trace=traceback.format_exc())
            try:
                self._json({"error": "internal"}, 500)
            except Exception:  # noqa: BLE001 - response may be partially sent
                pass
        finally:
            ms = (time.time() - t0) * 1000
            log.emit(self.state_dir, src="server", kind="http", level="info", req_id=req_id,
                     method="POST", path=u.path, status=self._status, ms=ms)

    def _dispatch_post(self, u) -> None:
        if u.path != "/api/log":
            return self._json({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return self._json({"error": "bad content-length"}, 400)
        if n < 0 or n > 65536:  # reject negative (read(-1) would read to EOF) + oversize
            return self._json({"error": "too large"}, 413)
        try:
            payload = json.loads(self.rfile.read(n) or b"null")
        except (ValueError, TypeError):
            return self._json({"error": "bad json"}, 400)
        events = payload if isinstance(payload, list) else [payload]
        for ev in events[:100]:  # cap batch
            log.write_client_event(self.state_dir, ev)
        self._json({"ok": True})

    def log_message(self, *args: object) -> None:  # our structured log replaces stderr noise
        pass


def serve(cs_db_path: str, state_dir: str, port: int = 8799, min_interval: float = 1.0) -> None:
    Handler.cs_db_path = cs_db_path
    Handler.state_dir = state_dir
    Handler.min_interval = min_interval
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    where = cs_db_path or "<no CS DB found>"
    print(f"provenance-gate -> http://127.0.0.1:{port}  (state {state_dir}, lazy; CS {where})")
    httpd.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description="Provenance Gate server")
    ap.add_argument("--cs-db", default=default_cs_db(), help="path to operon-cli.db (read-only)")
    ap.add_argument(
        "--state-dir",
        default=str(workspace.default_state_dir()),
        help="per-project state dir (env PG_STATE_DIR; default .pg/)",
    )
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument(
        "--stale-after", type=float, default=1.0,
        help="re-derive a project at most once per N seconds (rapid reads serve the cache)",
    )
    a = ap.parse_args()
    serve(a.cs_db, a.state_dir, a.port, a.stale_after)


if __name__ == "__main__":
    main()
