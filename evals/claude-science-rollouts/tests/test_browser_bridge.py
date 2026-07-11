from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from claude_science_rollouts.browser import (
    BrowserBridge,
    BrowserClient,
    BrowserProcessError,
    BrowserProtocolError,
    BrowserRequest,
    BrowserTimeoutError,
    make_request,
    parse_response,
)
from claude_science_rollouts.browser.protocol import (
    MAX_DEADLINE_MS,
    MAX_ERROR_EVIDENCE_BYTES,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    OPERATIONS,
    PROTOCOL_VERSION,
)

ROOT = Path(__file__).parents[1]
BROWSER_DIR = ROOT / "browser"
MOCK_BOUNDARY = BROWSER_DIR / "test" / "fixtures" / "mock_boundary.mjs"


def request() -> BrowserRequest:
    return make_request(
        "session.inspect",
        request_id="request-001",
        session_id="session-001",
        origin="http://127.0.0.1:8875",
        deadline_ms=15_000,
    )


def test_python_constants_match_canonical_protocol_spec() -> None:
    spec = json.loads((BROWSER_DIR / "protocol.json").read_text())
    assert PROTOCOL_VERSION == spec["protocol_version"]
    assert OPERATIONS == frozenset(spec["operations"])
    assert MAX_REQUEST_BYTES == spec["limits"]["request_bytes"]
    assert MAX_RESPONSE_BYTES == spec["limits"]["response_bytes"]
    assert MAX_ERROR_EVIDENCE_BYTES == spec["limits"]["error_evidence_bytes"]
    assert MAX_DEADLINE_MS == spec["limits"]["deadline_ms"]


def test_request_rejects_credentials_and_non_bare_origins() -> None:
    with pytest.raises(BrowserProtocolError, match="forbidden"):
        make_request(
            "project.inspect",
            request_id="request-001",
            session_id="session-001",
            origin="http://127.0.0.1:8875",
            deadline_ms=100,
            payload={"nested": {"token": "sensitive"}},
        )
    with pytest.raises(BrowserProtocolError, match="bare"):
        make_request(
            "project.inspect",
            request_id="request-001",
            session_id="session-001",
            origin="http://127.0.0.1:8875/project/1",
            deadline_ms=100,
        )


def test_request_rejects_non_canonical_origins() -> None:
    # Python (the request builder) must reject the same non-canonical origins Node does
    # (mixed case, default port present) rather than pass them to a Node-side rejection.
    for origin in ("HTTP://127.0.0.1:8875", "http://LOCALHOST:8875", "http://localhost:80"):
        with pytest.raises(BrowserProtocolError, match="bare"):
            make_request(
                "project.inspect",
                request_id="request-001",
                session_id="session-001",
                origin=origin,
                deadline_ms=100,
            )


def test_response_must_correlate_to_request() -> None:
    response = {
        "protocol_version": 1,
        "request_id": "different-request",
        "operation": "session.inspect",
        "outcome": "completed",
        "elapsed_ms": 1,
        "result": {},
    }
    with pytest.raises(BrowserProtocolError, match="request_id"):
        parse_response(json.dumps(response), request())


def test_unknown_outcome_cannot_be_retryable() -> None:
    response = {
        "protocol_version": 1,
        "request_id": "request-001",
        "operation": "session.inspect",
        "outcome": "unknown_outcome",
        "elapsed_ms": 1,
        "error": {"code": "AMBIGUOUS", "message": "Unknown", "retryable": True, "evidence": {}},
    }
    with pytest.raises(BrowserProtocolError, match="non-retryable"):
        parse_response(json.dumps(response), request())


def test_session_inspection_result_is_exact_and_typed() -> None:
    response = {
        "protocol_version": 1,
        "request_id": "request-001",
        "operation": "session.inspect",
        "outcome": "completed",
        "elapsed_ms": 1,
        "result": {
            "authenticated": "yes",
            "origin": "http://127.0.0.1:8875",
            "profile_ready": True,
        },
    }
    with pytest.raises(BrowserProtocolError, match="authenticated"):
        parse_response(json.dumps(response), request())


def test_real_python_to_node_mock_round_trip() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed")
    response = BrowserBridge((node, str(MOCK_BOUNDARY))).invoke(request())
    assert response.completed
    assert response.result == {
        "authenticated": True,
        "origin": "http://127.0.0.1:8875",
        "profile_ready": True,
    }


def test_typed_client_records_boundary_and_wall_time(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed")
    client = BrowserClient(
        bridge=BrowserBridge((node, str(MOCK_BOUNDARY)), cwd=tmp_path),
        session_id="session-001",
        origin="http://127.0.0.1:8875",
    )
    outcome = client.inspect_session(request_id="request-001")
    assert outcome.outcome == "completed"
    assert outcome.inspection is not None
    assert outcome.inspection.authenticated
    assert outcome.inspection.profile_ready
    assert outcome.boundary_elapsed_ms >= 0
    assert outcome.wall_elapsed_ms >= 0
    assert outcome.transport_overhead_ms >= 0


def test_typed_client_requires_explicit_absolute_working_directory() -> None:
    bridge = BrowserBridge((sys.executable, "unused.py"))
    with pytest.raises(ValueError, match="absolute working directory"):
        BrowserClient(
            bridge=bridge,
            session_id="session-001",
            origin="http://127.0.0.1:8875",
        )


def test_nonzero_process_is_not_replayed(tmp_path: Path) -> None:
    marker = tmp_path / "calls"
    script = tmp_path / "fail.py"
    script.write_text(
        "from pathlib import Path\n"
        f"p = Path({str(marker)!r})\n"
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x')\n"
        "raise SystemExit(3)\n"
    )
    with pytest.raises(BrowserProcessError) as exc_info:
        BrowserBridge((sys.executable, str(script))).invoke(request())
    assert exc_info.value.returncode == 3
    assert marker.read_text() == "x"


def test_timeout_is_not_replayed(tmp_path: Path) -> None:
    marker = tmp_path / "calls"
    script = tmp_path / "wait.py"
    script.write_text(
        "from pathlib import Path\n"
        "from time import sleep\n"
        f"p = Path({str(marker)!r})\n"
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x')\n"
        "sleep(2)\n"
    )
    short_request = make_request(
        "session.inspect",
        request_id="request-001",
        session_id="session-001",
        origin="http://127.0.0.1:8875",
        deadline_ms=25,
    )
    with pytest.raises(BrowserTimeoutError):
        BrowserBridge((sys.executable, str(script)), timeout_headroom_ms=0).invoke(short_request)
    assert marker.read_text() == "x"


def test_successful_process_must_keep_stderr_empty(tmp_path: Path) -> None:
    script = tmp_path / "noisy.py"
    script.write_text("import sys\nsys.stderr.write('noise')\nprint('{}')\n")
    with pytest.raises(BrowserProtocolError, match="stderr"):
        BrowserBridge((sys.executable, str(script))).invoke(request())
