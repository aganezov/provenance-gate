"""Event log: routes per-project vs global, blocks pid traversal, survives concurrent appends."""

import json
import threading

from provenance_gate import log


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_emit_routes_to_project_file(tmp_path):
    log.emit(str(tmp_path), src="server", kind="http", project_id="proj_x",
             path="/api/graph", status=200)
    f = tmp_path / "proj_x" / "events.jsonl"
    assert f.exists()
    (line,) = _lines(f)
    assert line["project_id"] == "proj_x" and line["kind"] == "http" and line["run_id"]


def test_emit_without_project_goes_to_session(tmp_path):
    log.emit(str(tmp_path), src="server", kind="op", name="watch.tick", projects=3)
    assert (tmp_path / "session.jsonl").exists()
    assert not (tmp_path / "None").exists()  # a None pid must not become a directory


def test_client_event_bad_pid_is_not_traversal(tmp_path):
    ev = {"kind": "action", "project_id": "../escape", "name": "x"}
    log.write_client_event(str(tmp_path), ev)
    assert (tmp_path / "session.jsonl").exists()  # unsafe pid falls back to session log
    assert not (tmp_path.parent / "escape").exists()  # nothing written outside the state dir


def test_concurrent_appends_are_intact(tmp_path):
    def worker(i):
        for j in range(20):
            log.emit(str(tmp_path), src="server", kind="op", project_id="proj_x", i=i, j=j)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = _lines(tmp_path / "proj_x" / "events.jsonl")
    assert len(lines) == 8 * 20  # no lost or torn writes under the lock
    assert all(isinstance(x, dict) for x in lines)


def test_client_cannot_forge_server_src(tmp_path):
    log.write_client_event(str(tmp_path), {"src": "server", "project_id": "proj_x"})
    (line,) = _lines(tmp_path / "proj_x" / "events.jsonl")
    assert line["src"] == "ui"  # a client POST cannot masquerade as a server-generated event
