"""Offline coverage for database-authoritative response, approval, and terminal-candidate reads.

Every case builds a temp-file operon (projects + one root frame + its JSON messages) and drives the
readers directly. No browser, no subprocess, no live operon — the reader is pure SQLite, so a static
file stands in for a settled snapshot. The one thing this cannot exercise offline is the barrier
racing a still-writing database: with a static file the first two polls always agree, so we only
see the already-stable path (attempts == 2), never a mid-write retry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from claude_science_rollouts.persistence.responses import (
    DatabaseResponseReader,
    PersistedResponseError,
    observe_persisted_input_request,
    observe_persisted_response,
    observe_persisted_terminal_candidate,
    read_persisted_input_request_snapshot,
    read_persisted_response_snapshot,
)
from claude_science_rollouts.persistence.snapshots import SnapshotBarrierConfig
from operon_fixture import FRAME_SCHEMA, frame_message

_PROJECT = "project-1"
_ROOT = "root-1"
_USER = "user-1"
_PROMPT = "Perform the requested update."
_PROMPT_SHA = hashlib.sha256(_PROMPT.encode()).hexdigest()

_FAST_BARRIER = SnapshotBarrierConfig(poll_interval_seconds=0, timeout_seconds=2)

Message = tuple[str, str, list[dict[str, object]]]

_USER_TURN: Message = ("user", _USER, [{"type": "text", "text": _PROMPT}])
_INTERIM_TOOL_USE: Message = (
    "assistant",
    "assistant-tool",
    [{"type": "tool_use", "id": "python-1", "name": "python", "input": {}}],
)
_FINAL_PROSE: Message = (
    "assistant",
    "assistant-final",
    [{"type": "text", "text": "Update complete."}],
)


def _write_operon(
    path: Path, *, status: str, messages: list[Message], model: str = "research-test-v1"
) -> Path:
    """Materialize a one-project, one-root-frame operon with ``messages`` in idx order."""
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT);\n" + FRAME_SCHEMA)
    conn.execute("INSERT INTO projects VALUES(?, ?)", (_PROJECT, "test"))
    conn.execute(
        "INSERT INTO frames VALUES(?, ?, ?, ?, ?)", (_ROOT, _ROOT, _PROJECT, status, model)
    )
    conn.executemany(
        "INSERT INTO frame_messages VALUES(?, ?, ?, ?)",
        [
            (_ROOT, idx, frame_message(role, mid, content), mid)
            for idx, (role, mid, content) in enumerate(messages, start=1)
        ],
    )
    conn.commit()
    conn.close()
    return path


def _input_request_turn(
    payload: dict[str, object], *, name: str = "request_input"
) -> list[Message]:
    """A turn that ran one tool, narrated a conflict, then paused on a single approval ask."""
    return [
        _USER_TURN,
        (
            "assistant",
            "assistant-ask",
            [
                {"type": "text", "text": "I found a version conflict."},
                {"type": "tool_use", "id": "input-1", "name": name, "input": payload},
            ],
        ),
    ]


def _observe_response(conn: sqlite3.Connection):
    return observe_persisted_response(
        conn,
        _PROJECT,
        root_frame_id=_ROOT,
        user_turn_id=_USER,
        authored_prompt=_PROMPT,
        authored_prompt_sha256=_PROMPT_SHA,
    )


def _observe_input_request(conn: sqlite3.Connection):
    return observe_persisted_input_request(
        conn,
        _PROJECT,
        root_frame_id=_ROOT,
        user_turn_id=_USER,
        authored_prompt=_PROMPT,
        authored_prompt_sha256=_PROMPT_SHA,
    )


def _candidate(conn: sqlite3.Connection, **overrides: str):
    return observe_persisted_terminal_candidate(
        conn,
        overrides.get("project_id", _PROJECT),
        root_frame_id=overrides.get("root_frame_id", _ROOT),
        user_turn_id=overrides.get("user_turn_id", _USER),
        authored_prompt=_PROMPT,
        authored_prompt_sha256=_PROMPT_SHA,
    )


def _readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


# --- response observation -------------------------------------------------------------------------


def test_terminal_prose_after_tool_use_is_observed(tmp_path: Path) -> None:
    source = _write_operon(
        tmp_path / "live.db",
        status="completed",
        messages=[_USER_TURN, _INTERIM_TOOL_USE, _FINAL_PROSE],
    )
    conn = _readonly(source)
    try:
        observed = _observe_response(conn)
    finally:
        conn.close()
    assert observed.ready is True
    assert observed.assistant_message_id == "assistant-final"
    assert observed.assistant_text == "Update complete."
    assert observed.assistant_text_sha256 == hashlib.sha256(b"Update complete.").hexdigest()
    assert observed.user_text_sha256 == _PROMPT_SHA
    assert observed.root_model_identifier == "research-test-v1"


def test_system_notice_does_not_truncate_the_turn(tmp_path: Path) -> None:
    # Claude Science injects a user-role "[System] ..." message mid-turn; it must not bound it,
    # or the terminal prose after it is lost and the turn wrongly reads as not-ready.
    system_notice: Message = (
        "user",
        "system-1",
        [{"type": "text", "text": "[System] Your execution plan has been approved."}],
    )
    source = _write_operon(
        tmp_path / "live.db",
        status="completed",
        messages=[_USER_TURN, _INTERIM_TOOL_USE, system_notice, _FINAL_PROSE],
    )
    conn = _readonly(source)
    try:
        observed = _observe_response(conn)
    finally:
        conn.close()
    assert observed.ready is True
    assert observed.assistant_text == "Update complete."


def test_tool_use_without_terminal_prose_is_not_ready(tmp_path: Path) -> None:
    source = _write_operon(
        tmp_path / "live.db", status="completed", messages=[_USER_TURN, _INTERIM_TOOL_USE]
    )
    conn = _readonly(source)
    try:
        assert _observe_response(conn).ready is False
        # completed status but no settled prose yet is not a terminal candidate.
        assert _candidate(conn) is None
    finally:
        conn.close()


def test_interim_prose_before_last_tool_activity_is_not_the_answer(tmp_path: Path) -> None:
    interim: Message = (
        "assistant",
        "assistant-interim",
        [{"type": "text", "text": "Working on it."}],
    )
    source = _write_operon(
        tmp_path / "live.db",
        status="completed",
        messages=[_USER_TURN, interim, _INTERIM_TOOL_USE, _FINAL_PROSE],
    )
    conn = _readonly(source)
    try:
        observed = _observe_response(conn)
    finally:
        conn.close()
    # only the prose after the last tool activity counts, so the interim line is not mistaken for a
    # second terminal answer.
    assert observed.ready is True
    assert observed.assistant_message_id == "assistant-final"


def test_two_terminal_responses_fail_closed(tmp_path: Path) -> None:
    second: Message = (
        "assistant",
        "assistant-second",
        [{"type": "text", "text": "A second terminal answer."}],
    )
    source = _write_operon(
        tmp_path / "live.db",
        status="completed",
        messages=[_USER_TURN, _INTERIM_TOOL_USE, _FINAL_PROSE, second],
    )
    conn = _readonly(source)
    try:
        with pytest.raises(PersistedResponseError, match="ambiguous"):
            _observe_response(conn)
    finally:
        conn.close()


def test_oversized_response_is_rejected_not_truncated(tmp_path: Path) -> None:
    huge: Message = ("assistant", "assistant-final", [{"type": "text", "text": "x" * 32_769}])
    source = _write_operon(
        tmp_path / "live.db", status="completed", messages=[_USER_TURN, _INTERIM_TOOL_USE, huge]
    )
    conn = _readonly(source)
    try:
        with pytest.raises(PersistedResponseError, match="exceeds"):
            _observe_response(conn)
    finally:
        conn.close()


def test_authored_prompt_mismatch_fails_closed(tmp_path: Path) -> None:
    source = _write_operon(
        tmp_path / "live.db",
        status="completed",
        messages=[_USER_TURN, _INTERIM_TOOL_USE, _FINAL_PROSE],
    )
    conn = _readonly(source)
    try:
        with pytest.raises(PersistedResponseError, match="text mismatch"):
            observe_persisted_response(
                conn,
                _PROJECT,
                root_frame_id=_ROOT,
                user_turn_id=_USER,
                authored_prompt="a different prompt",
                authored_prompt_sha256=hashlib.sha256(b"a different prompt").hexdigest(),
            )
    finally:
        conn.close()


# --- terminal candidates --------------------------------------------------------------------------


def test_completed_response_is_a_response_candidate(tmp_path: Path) -> None:
    source = _write_operon(
        tmp_path / "live.db",
        status="completed",
        messages=[_USER_TURN, _INTERIM_TOOL_USE, _FINAL_PROSE],
    )
    conn = _readonly(source)
    try:
        candidate = _candidate(conn)
    finally:
        conn.close()
    assert candidate is not None
    assert candidate.kind == "response"
    assert candidate.root_status == "completed"


def test_running_root_is_not_a_terminal_candidate(tmp_path: Path) -> None:
    source = _write_operon(
        tmp_path / "live.db",
        status="running",
        messages=[_USER_TURN, _INTERIM_TOOL_USE, _FINAL_PROSE],
    )
    conn = _readonly(source)
    try:
        assert _candidate(conn) is None
    finally:
        conn.close()


def test_failed_root_is_a_failure_candidate(tmp_path: Path) -> None:
    source = _write_operon(
        tmp_path / "live.db", status="failed", messages=[_USER_TURN, _INTERIM_TOOL_USE]
    )
    conn = _readonly(source)
    try:
        candidate = _candidate(conn)
    finally:
        conn.close()
    assert candidate is not None
    assert candidate.kind == "failure"
    assert candidate.root_status == "failed"


def test_wrong_identity_is_absent_or_fails_closed(tmp_path: Path) -> None:
    source = _write_operon(
        tmp_path / "live.db",
        status="completed",
        messages=[_USER_TURN, _INTERIM_TOOL_USE, _FINAL_PROSE],
    )
    conn = _readonly(source)
    try:
        # a missing frame or turn simply never reconciles; a mismatched project is a hard error.
        assert _candidate(conn, root_frame_id="root-missing") is None
        assert _candidate(conn, user_turn_id="user-missing") is None
        with pytest.raises(PersistedResponseError, match="root identity mismatch"):
            _candidate(conn, project_id="project-other")
    finally:
        conn.close()


# --- input requests -------------------------------------------------------------------------------


def test_pending_input_request_is_observed_with_payload_and_prose(tmp_path: Path) -> None:
    payload = {
        "question": "The outputs use different QC versions. Continue?",
        "choices": ["Stop", "Go"],
    }
    source = _write_operon(
        tmp_path / "live.db",
        status="awaiting_user_response",
        messages=_input_request_turn(payload),
    )
    conn = _readonly(source)
    try:
        observed = _observe_input_request(conn)
        candidate = _candidate(conn)
    finally:
        conn.close()
    assert observed.ready is True
    assert observed.assistant_message_id == "assistant-ask"
    assert observed.assistant_text == "I found a version conflict."
    assert observed.input_request_name == "request_input"
    assert observed.input_payload == payload
    assert observed.root_status == "awaiting_user_response"
    assert candidate is not None and candidate.kind == "input_request"


def test_ordinary_tool_use_is_not_an_input_request(tmp_path: Path) -> None:
    source = _write_operon(
        tmp_path / "live.db",
        status="awaiting_user_response",
        messages=[_USER_TURN, _INTERIM_TOOL_USE],
    )
    conn = _readonly(source)
    try:
        assert _observe_input_request(conn).ready is False
        assert _candidate(conn) is None
    finally:
        conn.close()


def test_resolved_input_request_is_no_longer_pending(tmp_path: Path) -> None:
    payload = {"question": "Continue?"}
    resolved: Message = (
        "user",
        "tool-result-1",
        [
            {
                "type": "tool_result",
                "tool_use_id": "input-1",
                "content": json.dumps({"status": "ok"}),
            }
        ],
    )
    source = _write_operon(
        tmp_path / "live.db",
        status="running",
        messages=[*_input_request_turn(payload), resolved],
    )
    conn = _readonly(source)
    try:
        # the tool_result classifies as resolved, so no request is still awaiting the user.
        assert _observe_input_request(conn).ready is False
    finally:
        conn.close()


def test_two_active_input_requests_fail_closed(tmp_path: Path) -> None:
    two_asks: Message = (
        "assistant",
        "assistant-ask",
        [
            {"type": "tool_use", "id": "input-1", "name": "request_input", "input": {"q": "first"}},
            {"type": "tool_use", "id": "input-2", "name": "ask_user", "input": {"q": "second"}},
        ],
    )
    source = _write_operon(
        tmp_path / "live.db",
        status="awaiting_user_response",
        messages=[_USER_TURN, two_asks],
    )
    conn = _readonly(source)
    try:
        with pytest.raises(PersistedResponseError, match="ambiguous"):
            _observe_input_request(conn)
    finally:
        conn.close()


def test_unresolved_input_request_blocks_terminal_prose(tmp_path: Path) -> None:
    trailing_prose: Message = (
        "assistant",
        "assistant-final",
        [{"type": "text", "text": "Meanwhile, here is a summary."}],
    )
    source = _write_operon(
        tmp_path / "live.db",
        status="awaiting_user_response",
        messages=[*_input_request_turn({"question": "Continue?"}), trailing_prose],
    )
    conn = _readonly(source)
    try:
        # a turn paused on an approval never reads as a completed response, even with trailing text.
        assert _observe_response(conn).ready is False
    finally:
        conn.close()


# --- content fingerprints -------------------------------------------------------------------------


def test_response_fingerprints_are_stable_across_reads(tmp_path: Path) -> None:
    messages = [_USER_TURN, _INTERIM_TOOL_USE, _FINAL_PROSE]
    first = _write_operon(tmp_path / "first.db", status="completed", messages=messages)
    second = _write_operon(tmp_path / "second.db", status="completed", messages=messages)
    conn_a, conn_b = _readonly(first), _readonly(second)
    try:
        observed_a = _observe_response(conn_a)
        observed_b = _observe_response(conn_b)
    finally:
        conn_a.close()
        conn_b.close()
    assert observed_a.assistant_text_sha256 == observed_b.assistant_text_sha256
    assert observed_a.user_text_sha256 == observed_b.user_text_sha256


def test_input_payload_fingerprint_is_canonical(tmp_path: Path) -> None:
    ordered = {"choices": ["Stop", "Go"], "question": "Continue?"}
    reordered = {"question": "Continue?", "choices": ["Stop", "Go"]}
    source_a = _write_operon(
        tmp_path / "a.db", status="awaiting_user_response", messages=_input_request_turn(ordered)
    )
    source_b = _write_operon(
        tmp_path / "b.db", status="awaiting_user_response", messages=_input_request_turn(reordered)
    )
    conn_a, conn_b = _readonly(source_a), _readonly(source_b)
    try:
        observed_a = _observe_input_request(conn_a)
        observed_b = _observe_input_request(conn_b)
    finally:
        conn_a.close()
        conn_b.close()
    canonical = json.dumps(ordered, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    # key order in the stored payload must not move the fingerprint.
    assert observed_a.input_payload_sha256 == observed_b.input_payload_sha256
    assert observed_a.input_payload_sha256 == hashlib.sha256(canonical.encode()).hexdigest()


# --- snapshot readers over a settled copy ---------------------------------------------------------


def test_snapshot_response_reader_lifts_a_settled_observation(tmp_path: Path) -> None:
    source = _write_operon(
        tmp_path / "snapshot.db",
        status="completed",
        messages=[_USER_TURN, _INTERIM_TOOL_USE, _FINAL_PROSE],
    )
    response = read_persisted_response_snapshot(
        source,
        _PROJECT,
        root_frame_id=_ROOT,
        user_turn_id=_USER,
        authored_prompt=_PROMPT,
        authored_prompt_sha256=_PROMPT_SHA,
        stability_attempts=3,
    )
    assert response.assistant_text == "Update complete."
    assert response.assistant_text_sha256 == hashlib.sha256(b"Update complete.").hexdigest()
    assert response.stability_attempts == 3
    # the reader-class delegator returns the same value.
    via_reader = DatabaseResponseReader().read_from_snapshot(
        snapshot_db=source,
        project_id=_PROJECT,
        root_frame_id=_ROOT,
        user_turn_id=_USER,
        authored_prompt=_PROMPT,
        authored_prompt_sha256=_PROMPT_SHA,
        stability_attempts=3,
    )
    assert via_reader == response


def test_snapshot_input_request_reader_lifts_a_settled_observation(tmp_path: Path) -> None:
    payload = {"question": "Continue?", "choices": ["Stop", "Go"]}
    source = _write_operon(
        tmp_path / "snapshot.db",
        status="awaiting_user_response",
        messages=_input_request_turn(payload),
    )
    request = read_persisted_input_request_snapshot(
        source,
        _PROJECT,
        root_frame_id=_ROOT,
        user_turn_id=_USER,
        authored_prompt=_PROMPT,
        authored_prompt_sha256=_PROMPT_SHA,
        stability_attempts=2,
    )
    assert request.input_request_name == "request_input"
    assert request.input_payload == payload
    assert request.stability_attempts == 2
    via_reader = DatabaseResponseReader().read_input_request_from_snapshot(
        snapshot_db=source,
        project_id=_PROJECT,
        root_frame_id=_ROOT,
        user_turn_id=_USER,
        authored_prompt=_PROMPT,
        authored_prompt_sha256=_PROMPT_SHA,
        stability_attempts=2,
    )
    assert via_reader == request


def test_empty_payload_input_request_is_lifted_not_dropped(tmp_path: Path) -> None:
    # a request_input tool that takes no arguments carries an empty payload dict. the observer marks
    # it ready, so the snapshot reader must lift it, not reject an empty-but-present payload.
    source = _write_operon(
        tmp_path / "snapshot.db",
        status="awaiting_user_response",
        messages=_input_request_turn({}),
    )
    conn = _readonly(source)
    try:
        assert _observe_input_request(conn).ready is True
    finally:
        conn.close()
    request = read_persisted_input_request_snapshot(
        source,
        _PROJECT,
        root_frame_id=_ROOT,
        user_turn_id=_USER,
        authored_prompt=_PROMPT,
        authored_prompt_sha256=_PROMPT_SHA,
        stability_attempts=2,
    )
    assert request.input_payload == {}
    assert request.input_request_name == "request_input"


def test_snapshot_reader_rejects_an_unready_turn(tmp_path: Path) -> None:
    source = _write_operon(
        tmp_path / "snapshot.db", status="completed", messages=[_USER_TURN, _INTERIM_TOOL_USE]
    )
    with pytest.raises(PersistedResponseError, match="incomplete"):
        read_persisted_response_snapshot(
            source,
            _PROJECT,
            root_frame_id=_ROOT,
            user_turn_id=_USER,
            authored_prompt=_PROMPT,
            authored_prompt_sha256=_PROMPT_SHA,
            stability_attempts=2,
        )


# --- barrier-driven reads over a static file (already-stable path) --------------------------------


def test_reader_awaits_stable_response_and_cleans_work_dir(tmp_path: Path) -> None:
    source = _write_operon(
        tmp_path / "live.db",
        status="completed",
        messages=[_USER_TURN, _INTERIM_TOOL_USE, _FINAL_PROSE],
    )
    work = tmp_path / "response-work"
    response = asyncio.run(
        DatabaseResponseReader().read(
            source_db=source,
            work_dir=work,
            project_id=_PROJECT,
            root_frame_id=_ROOT,
            user_turn_id=_USER,
            authored_prompt=_PROMPT,
            authored_prompt_sha256=_PROMPT_SHA,
            config=_FAST_BARRIER,
        )
    )
    assert response.assistant_message_id == "assistant-final"
    assert response.assistant_text == "Update complete."
    assert response.root_model_identifier == "research-test-v1"
    assert response.stability_attempts == 2
    assert not work.exists()


def test_reader_awaits_stable_input_request(tmp_path: Path) -> None:
    payload = {"question": "Continue?", "choices": ["Stop", "Go"]}
    source = _write_operon(
        tmp_path / "live.db",
        status="awaiting_user_response",
        messages=_input_request_turn(payload, name="ask_user"),
    )
    work = tmp_path / "request-work"
    request = asyncio.run(
        DatabaseResponseReader().read_input_request(
            source_db=source,
            work_dir=work,
            project_id=_PROJECT,
            root_frame_id=_ROOT,
            user_turn_id=_USER,
            authored_prompt=_PROMPT,
            authored_prompt_sha256=_PROMPT_SHA,
            config=_FAST_BARRIER,
        )
    )
    assert request.input_request_name == "ask_user"
    assert request.input_payload == payload
    assert request.assistant_text == "I found a version conflict."
    assert request.root_status == "awaiting_user_response"
    assert request.stability_attempts == 2
    assert not work.exists()


def test_confirm_terminal_candidate_returns_a_stable_failure(tmp_path: Path) -> None:
    source = _write_operon(
        tmp_path / "live.db", status="failed", messages=[_USER_TURN, _INTERIM_TOOL_USE]
    )
    work = tmp_path / "candidate-work"
    candidate = asyncio.run(
        DatabaseResponseReader().confirm_terminal_candidate(
            source_db=source,
            work_dir=work,
            project_id=_PROJECT,
            root_frame_id=_ROOT,
            user_turn_id=_USER,
            authored_prompt=_PROMPT,
            authored_prompt_sha256=_PROMPT_SHA,
            expected_kind="failure",
            config=_FAST_BARRIER,
        )
    )
    assert candidate.kind == "failure"
    assert candidate.root_status == "failed"
    assert candidate.stability_attempts == 2
    assert not work.exists()


def test_malformed_mid_turn_message_does_not_abort_the_read(tmp_path: Path) -> None:
    # a malformed sibling (content is not a list, e.g. an injected event) must not abort the read
    # of a well-formed turn; the scan predicates skip it instead of raising.
    malformed = ("user", "malformed-1", None)
    source = _write_operon(
        tmp_path / "live.db",
        status="completed",
        messages=[_USER_TURN, _INTERIM_TOOL_USE, malformed, _FINAL_PROSE],
    )
    conn = _readonly(source)
    try:
        observed = _observe_response(conn)
    finally:
        conn.close()
    assert observed.ready is True
    assert observed.assistant_text == "Update complete."
