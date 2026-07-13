"""Read one settled turn's terminal outcome out of a frozen operon snapshot.

A rollout turn ends in exactly one of three ways: the agent produces terminal prose, it pauses on a
single input request (an execution-approval ask), or the root frame fails. Grading happens after the
fact, so this layer never trusts the live, still-writing database — it reads an immutable copy and
fails closed the moment the evidence is ambiguous. Every observation is bound to the exact authored
user turn (matched by message identity and by a whitespace-normalized content match that must be
unique across the frame) and fingerprinted with sha256, so a later grader can prove it scored the
same bytes that were frozen here.

The reads split into three layers: ``observe_*`` functions take an open connection and return a
ready/not-ready observation; snapshot readers open a frozen copy read-only and lift a ready
observation into a public ``Persisted*`` result; and ``DatabaseResponseReader`` drives the snapshot
barrier so a live source is only read once its project rows have stabilized twice.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

from claude_science_rollouts.capture.evidence import canonical_json
from claude_science_rollouts.oracle.snapshot import open_readonly

from .snapshots import SnapshotBarrierConfig, StableSnapshot, await_stable_project_snapshot

# an assistant answer or a serialized input-request payload past this size is treated as evidence we
# cannot vouch for, so we reject rather than silently truncate.
_MAX_RESPONSE_BYTES = 32_768
_MAX_USER_MESSAGE_BYTES = 65_536
# the two tool names Claude Science uses to hand control back for an execution approval.
_INPUT_REQUEST_TOOL_NAMES = frozenset({"ask_user", "request_input"})
# root frame statuses that stand on their own as a terminal failure, no prose required.
_TERMINAL_FAILURE_STATUSES = frozenset({"cancelled", "canceled", "error", "failed"})

_ToolResultState = Literal["awaiting", "failed", "resolved"]
_T = TypeVar("_T")


class PersistedResponseError(RuntimeError):
    """The frozen database cannot prove one exact terminal outcome for the turn."""


@dataclass(frozen=True, slots=True)
class PersistedResponse:
    """One completed turn: the authored prompt's fingerprint and the terminal assistant prose."""

    project_id: str
    root_frame_id: str
    user_turn_id: str
    user_text_sha256: str
    assistant_message_id: str
    assistant_text: str
    assistant_text_sha256: str
    stability_attempts: int
    root_model_identifier: str | None = None


@dataclass(frozen=True, slots=True)
class PersistedInputRequest:
    """A turn paused on exactly one unresolved execution-approval request."""

    project_id: str
    root_frame_id: str
    user_turn_id: str
    user_text_sha256: str
    assistant_message_id: str
    assistant_text: str
    input_request_id: str
    input_request_name: str
    input_payload: dict[str, Any]
    input_payload_sha256: str
    stability_attempts: int
    root_model_identifier: str | None = None
    root_status: str | None = None


TerminalCandidateKind = Literal["response", "input_request", "failure"]


@dataclass(frozen=True, slots=True)
class PersistedTerminalCandidate:
    """A cheap project/root/turn-scoped signal that the turn has reached a terminal shape."""

    kind: TerminalCandidateKind
    project_id: str
    root_frame_id: str
    user_turn_id: str
    root_status: str
    stability_attempts: int = 0


@dataclass(frozen=True, slots=True)
class _ResponseObservation:
    ready: bool
    project_id: str
    root_frame_id: str
    user_turn_id: str
    user_text_sha256: str | None = None
    assistant_message_id: str | None = None
    assistant_text: str | None = None
    assistant_text_sha256: str | None = None
    root_model_identifier: str | None = None


@dataclass(frozen=True, slots=True)
class _InputRequestObservation:
    ready: bool
    project_id: str
    root_frame_id: str
    user_turn_id: str
    user_text_sha256: str | None = None
    assistant_message_id: str | None = None
    assistant_text: str | None = None
    input_request_id: str | None = None
    input_request_name: str | None = None
    input_payload: dict[str, Any] | None = None
    input_payload_sha256: str | None = None
    root_model_identifier: str | None = None
    root_status: str | None = None


@dataclass(frozen=True, slots=True)
class _TerminalCandidateObservation:
    ready: bool
    candidate: PersistedTerminalCandidate | None = None


class PersistedResponseReader(Protocol):
    async def read(
        self,
        *,
        source_db: Path,
        work_dir: Path,
        project_id: str,
        root_frame_id: str,
        user_turn_id: str,
        authored_prompt: str,
        authored_prompt_sha256: str,
        config: SnapshotBarrierConfig,
    ) -> PersistedResponse: ...

    async def read_input_request(
        self,
        *,
        source_db: Path,
        work_dir: Path,
        project_id: str,
        root_frame_id: str,
        user_turn_id: str,
        authored_prompt: str,
        authored_prompt_sha256: str,
        config: SnapshotBarrierConfig,
    ) -> PersistedInputRequest: ...

    def read_from_snapshot(
        self,
        *,
        snapshot_db: Path,
        project_id: str,
        root_frame_id: str,
        user_turn_id: str,
        authored_prompt: str,
        authored_prompt_sha256: str,
        stability_attempts: int,
    ) -> PersistedResponse: ...

    def read_input_request_from_snapshot(
        self,
        *,
        snapshot_db: Path,
        project_id: str,
        root_frame_id: str,
        user_turn_id: str,
        authored_prompt: str,
        authored_prompt_sha256: str,
        stability_attempts: int,
    ) -> PersistedInputRequest: ...

    async def confirm_terminal_candidate(
        self,
        *,
        source_db: Path,
        work_dir: Path,
        project_id: str,
        root_frame_id: str,
        user_turn_id: str,
        authored_prompt: str,
        authored_prompt_sha256: str,
        expected_kind: TerminalCandidateKind,
        config: SnapshotBarrierConfig,
    ) -> PersistedTerminalCandidate: ...


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _parse_message(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise PersistedResponseError("persisted message JSON is invalid") from exc
    if not isinstance(value, dict):
        raise PersistedResponseError("persisted message JSON is not an object")
    return value


def _content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """The message's content blocks, rejecting any shape we cannot read exactly."""
    content = message.get("content")
    if not isinstance(content, list):
        raise PersistedResponseError("persisted message content is not a list")
    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            raise PersistedResponseError("persisted message block is not an object")
        blocks.append(block)
    return blocks


def _message_text(message: dict[str, Any]) -> str:
    return "\n".join(_text_of(block) for block in _content_blocks(message) if _is_text(block))


def _is_text(block: dict[str, Any]) -> bool:
    return block.get("type") == "text"


def _text_of(block: dict[str, Any]) -> str:
    value = block.get("text")
    if not isinstance(value, str):
        raise PersistedResponseError("persisted text block is malformed")
    return value


def _has_block(message: dict[str, Any], block_type: str) -> bool:
    # non-raising predicate: a message whose content is not a list simply has no such block.
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == block_type for block in content
    )


def _has_tool_use(message: dict[str, Any]) -> bool:
    return _has_block(message, "tool_use")


def _has_tool_result(message: dict[str, Any]) -> bool:
    return _has_block(message, "tool_result")


def _assert_message_identity(message: dict[str, Any], message_id: object) -> None:
    # when the message carries its own uuid it must agree with the row it was stored under.
    embedded = message.get("_uuid")
    if embedded is not None and embedded != message_id:
        raise PersistedResponseError("persisted message embedded identity mismatch")


def _root_frame_messages(
    conn: sqlite3.Connection, project_id: str, root_frame_id: str
) -> list[tuple[object, object, object]] | None:
    """Return the root frame's messages in idx order, or ``None`` when the frame is not present."""
    root = conn.execute(
        "SELECT id, root_frame_id, project_id FROM frames WHERE id = ?", (root_frame_id,)
    ).fetchone()
    if root is None:
        return None
    # a root frame points at itself and belongs to the project we are grading; anything else means
    # we were handed the wrong identity and must not silently read someone else's transcript.
    if str(root[2]) != project_id or str(root[1]) != root_frame_id:
        raise PersistedResponseError("persisted response root identity mismatch")
    return conn.execute(
        "SELECT idx, msg_uuid, msg_json FROM frame_messages WHERE frame_id = ? ORDER BY idx",
        (root_frame_id,),
    ).fetchall()


def _scan_text(message: dict[str, Any]) -> str:
    # text for scanning predicates (delivery uniqueness, turn boundary): a malformed sibling is
    # simply not a match, so it yields "" rather than raising the way the authoritative
    # _message_text does for the turn's own content.
    try:
        return _message_text(message)
    except PersistedResponseError:
        return ""


def _delivers_prompt_once(raw: object, normalized_prompt: str) -> bool:
    # a genuine authored-prompt delivery is a user message (not a tool result) whose text carries
    # the normalized prompt exactly once. this runs over every row to prove delivery uniqueness,
    # so a malformed sibling must read as "not a delivery" instead of raising out of the scan.
    try:
        candidate = _parse_message(raw)
    except PersistedResponseError:
        return False
    if candidate.get("role") != "user" or _has_tool_result(candidate):
        return False
    return " ".join(_scan_text(candidate).split()).count(normalized_prompt) == 1


def _locate_authored_turn(
    rows: list[tuple[object, object, object]],
    user_turn_id: str,
    authored_prompt: str,
    authored_prompt_sha256: str,
) -> tuple[int, str] | None:
    """Pin the message that delivered the authored prompt; ``None`` until it is present."""
    matches = [row for row in rows if str(row[1]) == user_turn_id]
    if not matches:
        return None
    if len(matches) != 1:
        raise PersistedResponseError("persisted user turn identity is ambiguous")

    user_index, message_id, raw_user = matches[0]
    user = _parse_message(raw_user)
    if user.get("role") != "user" or str(message_id) != user_turn_id:
        raise PersistedResponseError("persisted user turn role or identity mismatch")
    _assert_message_identity(user, user_turn_id)

    user_text = _message_text(user)
    if not user_text or len(user_text.encode()) > _MAX_USER_MESSAGE_BYTES:
        raise PersistedResponseError("persisted user turn text exceeds the evidence bound")
    if _sha256(authored_prompt) != authored_prompt_sha256:
        raise PersistedResponseError("authored prompt hash mismatch")

    normalized_prompt = " ".join(authored_prompt.split())
    normalized_user_text = " ".join(user_text.split())
    if not normalized_prompt or normalized_user_text.count(normalized_prompt) != 1:
        raise PersistedResponseError("persisted user turn text mismatch")

    # the pinned turn must be the only delivery of this prompt in the whole frame, otherwise a
    # replayed prompt could be mistaken for the one we authored.
    delivered_by = [
        str(candidate_id)
        for _, candidate_id, raw in rows
        if _delivers_prompt_once(raw, normalized_prompt)
    ]
    if delivered_by != [user_turn_id]:
        raise PersistedResponseError("persisted authored prompt delivery is ambiguous")
    return int(user_index), user_text


def _is_system_notice(message: dict[str, Any]) -> bool:
    # Claude Science injects user-role "[System] ..." messages mid-turn (execution-approval notices,
    # plan updates); they are not the next authored turn, so they must not bound the turn we read.
    return message.get("role") == "user" and _message_text(message).lstrip().startswith("[System]")


def _messages_after(
    rows: list[tuple[object, object, object]], user_index: int
) -> list[tuple[object, object, dict[str, Any]]]:
    """Messages that followed the authored turn, up to the next authored user turn."""
    later: list[tuple[object, object, dict[str, Any]]] = []
    for index, later_id, raw in rows:
        if int(index) <= user_index:
            continue
        try:
            message = _parse_message(raw)
            _content_blocks(message)  # a malformed sibling is not a turn message; drop it below
        except PersistedResponseError:
            continue
        # a later user message with real prose (not a tool result) opens the next turn and bounds
        # this one; tool-result user messages and injected [System] notices belong to the turn we
        # are still reading.
        if (
            message.get("role") == "user"
            and _scan_text(message)
            and not _has_tool_result(message)
            and not _is_system_notice(message)
        ):
            break
        later.append((index, later_id, message))
    return later


def _turn_messages(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    root_frame_id: str,
    user_turn_id: str,
    authored_prompt: str,
    authored_prompt_sha256: str,
) -> tuple[str | None, list[tuple[object, object, dict[str, Any]]]]:
    """The authored turn's user-text fingerprint and the messages it produced.

    A ``None`` fingerprint means the turn has not landed yet (missing frame or missing user
    message); any structural contradiction raises instead.
    """
    rows = _root_frame_messages(conn, project_id, root_frame_id)
    if rows is None:
        return None, []
    located = _locate_authored_turn(rows, user_turn_id, authored_prompt, authored_prompt_sha256)
    if located is None:
        return None, []
    user_index, user_text = located
    return _sha256(user_text), _messages_after(rows, user_index)


def _root_model_identifier(conn: sqlite3.Connection, root_frame_id: str) -> str | None:
    # the model column is optional across operon revisions, so probe for it before selecting.
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(frames)")}
    if "model" not in columns:
        return None
    row = conn.execute("SELECT model FROM frames WHERE id = ?", (root_frame_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    value = str(row[0]).strip()
    return value or None


def _root_status(conn: sqlite3.Connection, root_frame_id: str) -> str | None:
    row = conn.execute("SELECT status FROM frames WHERE id = ?", (root_frame_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    value = str(row[0]).strip().lower()
    return value or None


def _tool_result_state(block: dict[str, Any]) -> _ToolResultState:
    """Classify an input-request tool result as still awaiting the user, failed, or resolved."""
    if block.get("is_error") is True:
        return "failed"
    content = block.get("content")
    parsed: object = content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            parsed = content
    if isinstance(parsed, dict):
        if parsed.get("error") is not None:
            return "failed"
        if parsed.get("status") == "awaiting_user_response":
            return "awaiting"
    return "resolved"


def _input_requests_in(message: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The input-request tool-use blocks in an assistant message, keyed by request id."""
    found: list[tuple[str, dict[str, Any]]] = []
    for block in _content_blocks(message):
        if block.get("type") != "tool_use" or block.get("name") not in _INPUT_REQUEST_TOOL_NAMES:
            continue
        request_id = block.get("id")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
            raise PersistedResponseError("persisted input request identity is malformed")
        found.append((request_id, block))
    return found


def _tool_results_in(message: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The tool-result blocks in a user message, keyed by the tool_use they answer."""
    return [
        (block["tool_use_id"], block)
        for block in _content_blocks(message)
        if block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str)
    ]


def observe_persisted_response(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    root_frame_id: str,
    user_turn_id: str,
    authored_prompt: str,
    authored_prompt_sha256: str,
) -> _ResponseObservation:
    """Observe one exact authored user turn and its terminal assistant prose."""
    user_sha256, later = _turn_messages(
        conn,
        project_id,
        root_frame_id=root_frame_id,
        user_turn_id=user_turn_id,
        authored_prompt=authored_prompt,
        authored_prompt_sha256=authored_prompt_sha256,
    )
    if user_sha256 is None:
        return _ResponseObservation(False, project_id, root_frame_id, user_turn_id)
    not_ready = _ResponseObservation(
        False, project_id, root_frame_id, user_turn_id, user_text_sha256=user_sha256
    )
    if not later:
        return not_ready

    # a turn paused on an input request has not produced terminal prose, even if some assistant text
    # trails the ask; treat it as not-ready so the input-request path owns it.
    pending_input = observe_persisted_input_request(
        conn,
        project_id,
        root_frame_id=root_frame_id,
        user_turn_id=user_turn_id,
        authored_prompt=authored_prompt,
        authored_prompt_sha256=authored_prompt_sha256,
    )
    if pending_input.ready:
        return not_ready

    # the terminal answer is the assistant prose after the last tool activity; earlier prose is
    # interim narration.
    last_activity = max(
        (
            position
            for position, (_, _, message) in enumerate(later)
            if _has_tool_use(message) or _has_tool_result(message)
        ),
        default=-1,
    )
    eligible = [
        (assistant_id, assistant, text)
        for position, (_, assistant_id, assistant) in enumerate(later)
        if position > last_activity
        and assistant.get("role") == "assistant"
        and not _has_tool_use(assistant)
        and (text := _message_text(assistant))
    ]
    if not eligible:
        return not_ready
    if len(eligible) != 1:
        raise PersistedResponseError("persisted terminal assistant response is ambiguous")

    assistant_id, assistant, assistant_text = eligible[0]
    if not isinstance(assistant_id, str) or not assistant_id:
        raise PersistedResponseError("persisted assistant identity is missing")
    _assert_message_identity(assistant, assistant_id)
    if len(assistant_text.encode()) > _MAX_RESPONSE_BYTES:
        raise PersistedResponseError("persisted assistant response exceeds the evidence bound")
    return _ResponseObservation(
        True,
        project_id,
        root_frame_id,
        user_turn_id,
        user_text_sha256=user_sha256,
        assistant_message_id=assistant_id,
        assistant_text=assistant_text,
        assistant_text_sha256=_sha256(assistant_text),
        root_model_identifier=_root_model_identifier(conn, root_frame_id),
    )


def observe_persisted_input_request(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    root_frame_id: str,
    user_turn_id: str,
    authored_prompt: str,
    authored_prompt_sha256: str,
) -> _InputRequestObservation:
    """Observe one exact delivered turn paused on a single unresolved input request."""
    user_sha256, later = _turn_messages(
        conn,
        project_id,
        root_frame_id=root_frame_id,
        user_turn_id=user_turn_id,
        authored_prompt=authored_prompt,
        authored_prompt_sha256=authored_prompt_sha256,
    )
    if user_sha256 is None or not later:
        return _InputRequestObservation(
            False, project_id, root_frame_id, user_turn_id, user_text_sha256=user_sha256
        )

    assistant_texts: list[str] = []
    requests: dict[str, tuple[str, dict[str, Any]]] = {}
    results: dict[str, _ToolResultState] = {}
    for _, message_id, message in later:
        role = message.get("role")
        if role == "assistant":
            _assert_message_identity(message, message_id)
            text = _message_text(message)
            if text:
                assistant_texts.append(text)
            for request_id, block in _input_requests_in(message):
                if request_id in requests:
                    raise PersistedResponseError("persisted input request identity is ambiguous")
                if not isinstance(message_id, str) or not message_id:
                    raise PersistedResponseError("persisted assistant identity is missing")
                requests[request_id] = (message_id, block)
        elif role == "user":
            for request_id, block in _tool_results_in(message):
                if request_id not in requests:
                    continue
                if request_id in results:
                    raise PersistedResponseError("persisted input request result is ambiguous")
                results[request_id] = _tool_result_state(block)

    # a request with no recorded result, or one still explicitly awaiting, is what pauses the turn.
    pending = [
        (request_id, assistant_id, block)
        for request_id, (assistant_id, block) in requests.items()
        if results.get(request_id, "awaiting") == "awaiting"
    ]
    if not pending:
        return _InputRequestObservation(
            False, project_id, root_frame_id, user_turn_id, user_text_sha256=user_sha256
        )
    if len(pending) != 1:
        raise PersistedResponseError("persisted input request is ambiguous")
    request_id, assistant_id, block = pending[0]

    assistant_text = "\n".join(assistant_texts)
    if len(assistant_text.encode()) > _MAX_RESPONSE_BYTES:
        raise PersistedResponseError("persisted assistant response exceeds the evidence bound")
    request_name = block.get("name")
    payload = block.get("input")
    if (
        not isinstance(request_name, str)
        or not request_name
        or len(request_name) > 256
        or not isinstance(payload, dict)
    ):
        raise PersistedResponseError("persisted input request is malformed")
    payload_json = canonical_json(payload)
    if len(payload_json.encode()) > _MAX_RESPONSE_BYTES:
        raise PersistedResponseError("persisted input request exceeds the evidence bound")
    return _InputRequestObservation(
        True,
        project_id,
        root_frame_id,
        user_turn_id,
        user_text_sha256=user_sha256,
        assistant_message_id=assistant_id,
        assistant_text=assistant_text,
        input_request_id=request_id,
        input_request_name=request_name,
        input_payload=payload,
        input_payload_sha256=_sha256(payload_json),
        root_model_identifier=_root_model_identifier(conn, root_frame_id),
        root_status=_root_status(conn, root_frame_id),
    )


def observe_persisted_terminal_candidate(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    root_frame_id: str,
    user_turn_id: str,
    authored_prompt: str,
    authored_prompt_sha256: str,
) -> PersistedTerminalCandidate | None:
    """Cheaply classify the turn's terminal shape from the root status and the settled messages."""
    root = conn.execute(
        "SELECT id, root_frame_id, project_id, status FROM frames WHERE id = ?", (root_frame_id,)
    ).fetchone()
    if root is None:
        return None
    if (
        str(root[0]) != root_frame_id
        or str(root[1]) != root_frame_id
        or str(root[2]) != project_id
    ):
        raise PersistedResponseError("terminal candidate root identity mismatch")
    root_status = str(root[3]).strip().lower()

    user_sha256, _ = _turn_messages(
        conn,
        project_id,
        root_frame_id=root_frame_id,
        user_turn_id=user_turn_id,
        authored_prompt=authored_prompt,
        authored_prompt_sha256=authored_prompt_sha256,
    )
    if user_sha256 is None:
        return None

    if root_status in _TERMINAL_FAILURE_STATUSES:
        return PersistedTerminalCandidate(
            "failure", project_id, root_frame_id, user_turn_id, root_status
        )
    if root_status == "awaiting_user_response":
        pending_input = observe_persisted_input_request(
            conn,
            project_id,
            root_frame_id=root_frame_id,
            user_turn_id=user_turn_id,
            authored_prompt=authored_prompt,
            authored_prompt_sha256=authored_prompt_sha256,
        )
        if pending_input.ready:
            return PersistedTerminalCandidate(
                "input_request", project_id, root_frame_id, user_turn_id, root_status
            )
        return None
    if root_status != "completed":
        return None

    response = observe_persisted_response(
        conn,
        project_id,
        root_frame_id=root_frame_id,
        user_turn_id=user_turn_id,
        authored_prompt=authored_prompt,
        authored_prompt_sha256=authored_prompt_sha256,
    )
    if not response.ready:
        return None
    return PersistedTerminalCandidate(
        "response", project_id, root_frame_id, user_turn_id, root_status
    )


def _response_from_observation(
    observed: _ResponseObservation, stability_attempts: int
) -> PersistedResponse:
    # a present-but-empty prose string is still evidence; only a None field is a real gap the
    # observer left behind, so we test presence, not truthiness.
    required = (
        observed.user_text_sha256,
        observed.assistant_message_id,
        observed.assistant_text,
        observed.assistant_text_sha256,
    )
    if not observed.ready or any(field is None for field in required):
        raise PersistedResponseError("stable response observation is incomplete")
    return PersistedResponse(
        observed.project_id,
        observed.root_frame_id,
        observed.user_turn_id,
        observed.user_text_sha256,
        observed.assistant_message_id,
        observed.assistant_text,
        observed.assistant_text_sha256,
        stability_attempts,
        observed.root_model_identifier,
    )


def _input_request_from_observation(
    observed: _InputRequestObservation, stability_attempts: int
) -> PersistedInputRequest:
    # an input request can carry an empty payload dict (a tool that takes no arguments), which the
    # observer accepts as ready; only a None field is a real gap, so test presence, not truthiness.
    required = (
        observed.user_text_sha256,
        observed.assistant_message_id,
        observed.input_request_id,
        observed.input_request_name,
        observed.input_payload,
        observed.input_payload_sha256,
    )
    if not observed.ready or any(field is None for field in required):
        raise PersistedResponseError("stable input-request observation is incomplete")
    return PersistedInputRequest(
        observed.project_id,
        observed.root_frame_id,
        observed.user_turn_id,
        observed.user_text_sha256,
        observed.assistant_message_id,
        observed.assistant_text or "",
        observed.input_request_id,
        observed.input_request_name,
        observed.input_payload,
        observed.input_payload_sha256,
        stability_attempts,
        observed.root_model_identifier,
        observed.root_status,
    )


def _observe_readonly(db: str | Path, observe: Callable[[sqlite3.Connection], _T]) -> _T:
    """Open a frozen snapshot strictly read-only, observe it once, and always close the handle."""
    conn = open_readonly(Path(db).resolve())
    try:
        return observe(conn)
    finally:
        conn.close()


def probe_persisted_terminal_candidate(
    source_db: Path,
    project_id: str,
    *,
    root_frame_id: str,
    user_turn_id: str,
    authored_prompt: str,
    authored_prompt_sha256: str,
) -> PersistedTerminalCandidate | None:
    """Probe the live source strictly read-only for a terminal-candidate signal."""
    return _observe_readonly(
        source_db,
        lambda conn: observe_persisted_terminal_candidate(
            conn,
            project_id,
            root_frame_id=root_frame_id,
            user_turn_id=user_turn_id,
            authored_prompt=authored_prompt,
            authored_prompt_sha256=authored_prompt_sha256,
        ),
    )


def read_persisted_response_snapshot(
    snapshot_db: Path,
    project_id: str,
    *,
    root_frame_id: str,
    user_turn_id: str,
    authored_prompt: str,
    authored_prompt_sha256: str,
    stability_attempts: int,
) -> PersistedResponse:
    """Read one exact completed response from an already-stable reduced snapshot."""
    observed = _observe_readonly(
        snapshot_db,
        lambda conn: observe_persisted_response(
            conn,
            project_id,
            root_frame_id=root_frame_id,
            user_turn_id=user_turn_id,
            authored_prompt=authored_prompt,
            authored_prompt_sha256=authored_prompt_sha256,
        ),
    )
    return _response_from_observation(observed, stability_attempts)


def read_persisted_input_request_snapshot(
    snapshot_db: Path,
    project_id: str,
    *,
    root_frame_id: str,
    user_turn_id: str,
    authored_prompt: str,
    authored_prompt_sha256: str,
    stability_attempts: int,
) -> PersistedInputRequest:
    """Read one exact pending input request from an already-stable reduced snapshot."""
    observed = _observe_readonly(
        snapshot_db,
        lambda conn: observe_persisted_input_request(
            conn,
            project_id,
            root_frame_id=root_frame_id,
            user_turn_id=user_turn_id,
            authored_prompt=authored_prompt,
            authored_prompt_sha256=authored_prompt_sha256,
        ),
    )
    return _input_request_from_observation(observed, stability_attempts)


class DatabaseResponseReader:
    """Drive the snapshot-stability barrier and lift one exact terminal outcome once it is present
    and unchanged across two observations. The barrier must honor observation readiness — a
    ``ready=False`` observation is not a settled outcome — so the caller drives the turn to a
    terminal candidate before reading, rather than relying on this to wait a running turn out."""

    async def _await_stable(
        self,
        *,
        source_db: Path,
        work_dir: Path,
        project_id: str,
        config: SnapshotBarrierConfig,
        observer: Callable[[sqlite3.Connection, str], _T],
    ) -> StableSnapshot[_T]:
        """Drive the barrier over a private scratch dir and leave no snapshots behind."""
        work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work_dir) as scratch:
            stable = await await_stable_project_snapshot(
                source_db, project_id, Path(scratch), config=config, observer=observer
            )
        if work_dir.exists() and not any(work_dir.iterdir()):
            work_dir.rmdir()
        return stable

    async def read(
        self,
        *,
        source_db: Path,
        work_dir: Path,
        project_id: str,
        root_frame_id: str,
        user_turn_id: str,
        authored_prompt: str,
        authored_prompt_sha256: str,
        config: SnapshotBarrierConfig,
    ) -> PersistedResponse:
        def observer(conn: sqlite3.Connection, observed_project_id: str) -> _ResponseObservation:
            candidate = observe_persisted_terminal_candidate(
                conn,
                observed_project_id,
                root_frame_id=root_frame_id,
                user_turn_id=user_turn_id,
                authored_prompt=authored_prompt,
                authored_prompt_sha256=authored_prompt_sha256,
            )
            if candidate is None or candidate.kind != "response":
                return _ResponseObservation(
                    False, observed_project_id, root_frame_id, user_turn_id
                )
            return observe_persisted_response(
                conn,
                observed_project_id,
                root_frame_id=root_frame_id,
                user_turn_id=user_turn_id,
                authored_prompt=authored_prompt,
                authored_prompt_sha256=authored_prompt_sha256,
            )

        stable = await self._await_stable(
            source_db=source_db,
            work_dir=work_dir,
            project_id=project_id,
            config=config,
            observer=observer,
        )
        return _response_from_observation(stable.observation, stable.attempts)

    async def read_input_request(
        self,
        *,
        source_db: Path,
        work_dir: Path,
        project_id: str,
        root_frame_id: str,
        user_turn_id: str,
        authored_prompt: str,
        authored_prompt_sha256: str,
        config: SnapshotBarrierConfig,
    ) -> PersistedInputRequest:
        def observer(
            conn: sqlite3.Connection, observed_project_id: str
        ) -> _InputRequestObservation:
            candidate = observe_persisted_terminal_candidate(
                conn,
                observed_project_id,
                root_frame_id=root_frame_id,
                user_turn_id=user_turn_id,
                authored_prompt=authored_prompt,
                authored_prompt_sha256=authored_prompt_sha256,
            )
            if candidate is None or candidate.kind != "input_request":
                return _InputRequestObservation(
                    False, observed_project_id, root_frame_id, user_turn_id
                )
            return observe_persisted_input_request(
                conn,
                observed_project_id,
                root_frame_id=root_frame_id,
                user_turn_id=user_turn_id,
                authored_prompt=authored_prompt,
                authored_prompt_sha256=authored_prompt_sha256,
            )

        stable = await self._await_stable(
            source_db=source_db,
            work_dir=work_dir,
            project_id=project_id,
            config=config,
            observer=observer,
        )
        return _input_request_from_observation(stable.observation, stable.attempts)

    async def confirm_terminal_candidate(
        self,
        *,
        source_db: Path,
        work_dir: Path,
        project_id: str,
        root_frame_id: str,
        user_turn_id: str,
        authored_prompt: str,
        authored_prompt_sha256: str,
        expected_kind: TerminalCandidateKind,
        config: SnapshotBarrierConfig,
    ) -> PersistedTerminalCandidate:
        def observer(
            conn: sqlite3.Connection, observed_project_id: str
        ) -> _TerminalCandidateObservation:
            candidate = observe_persisted_terminal_candidate(
                conn,
                observed_project_id,
                root_frame_id=root_frame_id,
                user_turn_id=user_turn_id,
                authored_prompt=authored_prompt,
                authored_prompt_sha256=authored_prompt_sha256,
            )
            return _TerminalCandidateObservation(
                candidate is not None and candidate.kind == expected_kind, candidate
            )

        stable = await self._await_stable(
            source_db=source_db,
            work_dir=work_dir,
            project_id=project_id,
            config=config,
            observer=observer,
        )
        candidate = stable.observation.candidate
        if not stable.observation.ready or candidate is None or candidate.kind != expected_kind:
            raise PersistedResponseError("stable terminal candidate is incomplete")
        return PersistedTerminalCandidate(
            candidate.kind,
            candidate.project_id,
            candidate.root_frame_id,
            candidate.user_turn_id,
            candidate.root_status,
            stable.attempts,
        )

    def read_from_snapshot(
        self,
        *,
        snapshot_db: Path,
        project_id: str,
        root_frame_id: str,
        user_turn_id: str,
        authored_prompt: str,
        authored_prompt_sha256: str,
        stability_attempts: int,
    ) -> PersistedResponse:
        return read_persisted_response_snapshot(
            snapshot_db,
            project_id,
            root_frame_id=root_frame_id,
            user_turn_id=user_turn_id,
            authored_prompt=authored_prompt,
            authored_prompt_sha256=authored_prompt_sha256,
            stability_attempts=stability_attempts,
        )

    def read_input_request_from_snapshot(
        self,
        *,
        snapshot_db: Path,
        project_id: str,
        root_frame_id: str,
        user_turn_id: str,
        authored_prompt: str,
        authored_prompt_sha256: str,
        stability_attempts: int,
    ) -> PersistedInputRequest:
        return read_persisted_input_request_snapshot(
            snapshot_db,
            project_id,
            root_frame_id=root_frame_id,
            user_turn_id=user_turn_id,
            authored_prompt=authored_prompt,
            authored_prompt_sha256=authored_prompt_sha256,
            stability_attempts=stability_attempts,
        )
