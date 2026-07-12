"""Strict Python representation of the versioned browser JSON protocol."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit

from claude_science_rollouts.browser.errors import BrowserProtocolError

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 131_072
MAX_RESPONSE_BYTES = 262_144
MAX_ERROR_EVIDENCE_BYTES = 8_192
MAX_DEADLINE_MS = 900_000

OPERATIONS = frozenset(
    {
        "session.attach",
        "session.inspect",
        "session.detach",
        "project.inspect",
        "project.create",
        "attachment.upload",
        "agent_context.inspect",
        "agent_context.update",
        "chat.new",
        "chat.open",
        "chat.inspect",
        "turn.submit_wait",
        "approval.resolve",
    }
)
OUTCOMES = frozenset({"not_started", "completed", "unknown_outcome"})

Outcome = Literal["not_started", "completed", "unknown_outcome"]
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_TURN_STATES = frozenset(
    {
        "busy",
        "settled",
        "approval_required",
        "input_required",
        "indeterminate",
        "navigation_drift",
        "failed",
    }
)
_MAX_TRANSCRIPT_TURNS = 256
_MAX_APPROVAL_CARDS = 8
_MAX_ENABLED_SKILLS = 256
_MAX_TURN_TEXT_BYTES = 16_384
_CREDENTIAL_KEYS = frozenset(
    {"authorization", "cookie", "credential", "credentials", "password", "secret", "token"}
)
_REQUEST_KEYS = frozenset(
    {"protocol_version", "request_id", "operation", "session", "deadline_ms", "payload"}
)
_RESPONSE_KEYS = frozenset({"protocol_version", "request_id", "operation", "outcome", "elapsed_ms"})
_ERROR_KEYS = frozenset({"code", "message", "retryable", "evidence"})


@dataclass(frozen=True)
class BrowserRequest:
    request_id: str
    operation: str
    session_id: str
    origin: str
    deadline_ms: int
    payload: Mapping[str, Any]
    protocol_version: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "operation": self.operation,
            "session": {"session_id": self.session_id, "origin": self.origin},
            "deadline_ms": self.deadline_ms,
            "payload": dict(self.payload),
        }

    def to_json(self) -> str:
        text = _compact_json(self.to_dict(), "request")
        if len(text.encode()) > MAX_REQUEST_BYTES:
            raise BrowserProtocolError("Request exceeds the protocol byte limit")
        return text


@dataclass(frozen=True)
class BrowserError:
    code: str
    message: str
    retryable: bool
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class BrowserResponse:
    request_id: str
    operation: str
    outcome: Outcome
    elapsed_ms: int
    result: Mapping[str, Any] | None = None
    error: BrowserError | None = None
    protocol_version: int = PROTOCOL_VERSION

    @property
    def completed(self) -> bool:
        return self.outcome == "completed"


def make_request(
    operation: str,
    *,
    request_id: str,
    session_id: str,
    origin: str,
    deadline_ms: int,
    payload: Mapping[str, Any] | None = None,
) -> BrowserRequest:
    """Construct and validate one request without adding retry behavior."""
    body = {} if payload is None else dict(payload)
    _identifier(request_id, "request_id")
    _identifier(session_id, "session_id")
    if operation not in OPERATIONS:
        raise BrowserProtocolError("Operation is unsupported")
    _bare_http_origin(origin)
    _bounded_integer(deadline_ms, 1, MAX_DEADLINE_MS, "deadline_ms")
    _reject_credential_keys(body, "payload")
    if operation in {"session.attach", "session.inspect", "session.detach"} and body:
        raise BrowserProtocolError(f"{operation} payload must be empty")
    if operation in {"project.inspect", "agent_context.inspect"}:
        _exact_keys(body, {"project_id"}, set(), "payload")
        _identifier(body["project_id"], "payload.project_id")
    if operation == "chat.inspect":
        _exact_keys(body, {"project_id", "chat_id"}, {"root_frame_id"}, "payload")
        _identifier(body["project_id"], "payload.project_id")
        _identifier(body["chat_id"], "payload.chat_id")
        if "root_frame_id" in body:
            _identifier(body["root_frame_id"], "payload.root_frame_id")
    request = BrowserRequest(
        request_id=request_id,
        operation=operation,
        session_id=session_id,
        origin=origin,
        deadline_ms=deadline_ms,
        payload=MappingProxyType(body),
    )
    request.to_json()
    return request


def parse_response(text: str, request: BrowserRequest) -> BrowserResponse:
    """Parse, validate, and correlate exactly one response to its request."""
    if not isinstance(text, str):
        raise BrowserProtocolError("Response must be text")
    if len(text.encode()) > MAX_RESPONSE_BYTES:
        raise BrowserProtocolError("Response exceeds the protocol byte limit")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BrowserProtocolError("Response is not valid JSON") from exc
    response = _mapping(value, "response")
    _exact_keys(response, _RESPONSE_KEYS, {"result", "error"}, "response")
    _reject_credential_keys(response, "response")
    if response["protocol_version"] != PROTOCOL_VERSION:
        raise BrowserProtocolError("Response protocol version does not match")
    _identifier(response["request_id"], "response.request_id")
    if response["request_id"] != request.request_id:
        raise BrowserProtocolError("Response request_id does not match request")
    if response["operation"] != request.operation:
        raise BrowserProtocolError("Response operation does not match request")
    outcome = response["outcome"]
    if outcome not in OUTCOMES:
        raise BrowserProtocolError("Response outcome is unsupported")
    elapsed_ms = _bounded_integer(response["elapsed_ms"], 0, MAX_DEADLINE_MS, "response.elapsed_ms")

    if outcome == "completed":
        if "result" not in response or "error" in response:
            raise BrowserProtocolError("Completed response must contain only result")
        result_value = dict(_mapping(response["result"], "response.result"))
        _validate_operation_result(request.operation, result_value)
        result = MappingProxyType(result_value)
        return BrowserResponse(
            request_id=request.request_id,
            operation=request.operation,
            outcome="completed",
            elapsed_ms=elapsed_ms,
            result=result,
        )

    if "error" not in response or "result" in response:
        raise BrowserProtocolError("Non-completed response must contain only error")
    error = _parse_error(response["error"], outcome)
    return BrowserResponse(
        request_id=request.request_id,
        operation=request.operation,
        outcome=outcome,
        elapsed_ms=elapsed_ms,
        error=error,
    )


def _validate_operation_result(operation: str, result: Mapping[str, Any]) -> None:
    if operation == "session.detach":
        _exact_keys(result, {"detached"}, set(), "response.result")
        if not isinstance(result["detached"], bool):
            raise BrowserProtocolError("response.result.detached must be boolean")
        return
    if operation == "project.inspect":
        _validate_project_observation(result)
        return
    if operation == "chat.inspect":
        _validate_chat_observation(result)
        return
    if operation == "agent_context.inspect":
        _validate_context_observation(result)
        return
    if operation not in {"session.attach", "session.inspect"}:
        return
    _exact_keys(
        result,
        {"authenticated", "origin", "profile_ready"},
        set(),
        "response.result",
    )
    if not isinstance(result["authenticated"], bool):
        raise BrowserProtocolError("response.result.authenticated must be boolean")
    _bare_http_origin(result["origin"])
    if not isinstance(result["profile_ready"], bool):
        raise BrowserProtocolError("response.result.profile_ready must be boolean")


def _validate_project_observation(result: Mapping[str, Any]) -> None:
    _exact_keys(
        result,
        {
            "project_id",
            "verified",
            "composer_empty",
            "user_turn_count",
            "root_frame_id",
            "root_state",
        },
        set(),
        "response.result",
    )
    _identifier(result["project_id"], "response.result.project_id")
    _boolean(result["verified"], "response.result.verified")
    _boolean(result["composer_empty"], "response.result.composer_empty")
    _bounded_integer(result["user_turn_count"], 0, 1_000_000, "response.result.user_turn_count")
    _nullable_identifier(result["root_frame_id"], "response.result.root_frame_id")
    _nullable_identifier(result["root_state"], "response.result.root_state")
    if (result["root_frame_id"] is None) != (result["root_state"] is None):
        raise BrowserProtocolError(
            "Project root identity and state must be jointly present or absent"
        )


def _validate_chat_observation(result: Mapping[str, Any]) -> None:
    _exact_keys(
        result,
        {
            "project_id",
            "chat_id",
            "transcript",
            "user_turn_count",
            "composer_empty",
            "root_frame_id",
            "response_control_id",
            "current_turn_state",
            "approval_cards",
        },
        set(),
        "response.result",
    )
    _identifier(result["project_id"], "response.result.project_id")
    _identifier(result["chat_id"], "response.result.chat_id")
    transcript = _bounded_list(
        result["transcript"], _MAX_TRANSCRIPT_TURNS, "response.result.transcript"
    )
    turn_ids: set[str] = set()
    observed_users = 0
    for index, value in enumerate(transcript):
        path = f"response.result.transcript[{index}]"
        turn = _mapping(value, path)
        _exact_keys(turn, {"turn_id", "role", "text", "truncated"}, set(), path)
        turn_id = _identifier(turn["turn_id"], f"{path}.turn_id")
        if turn_id in turn_ids:
            raise BrowserProtocolError("Transcript turn IDs must be unique")
        turn_ids.add(turn_id)
        if turn["role"] not in {"user", "assistant"}:
            raise BrowserProtocolError(f"{path}.role is invalid")
        if turn["role"] == "user":
            observed_users += 1
        _bounded_string(turn["text"], _MAX_TURN_TEXT_BYTES, f"{path}.text")
        _boolean(turn["truncated"], f"{path}.truncated")
    count = _bounded_integer(
        result["user_turn_count"], 0, 1_000_000, "response.result.user_turn_count"
    )
    if count != observed_users:
        raise BrowserProtocolError("Chat user-turn count contradicts the transcript")
    _boolean(result["composer_empty"], "response.result.composer_empty")
    _nullable_identifier(result["root_frame_id"], "response.result.root_frame_id")
    response_control_id = _nullable_identifier(
        result["response_control_id"], "response.result.response_control_id"
    )
    if result["current_turn_state"] not in _TURN_STATES:
        raise BrowserProtocolError("response.result.current_turn_state is invalid")
    cards = _bounded_list(
        result["approval_cards"], _MAX_APPROVAL_CARDS, "response.result.approval_cards"
    )
    card_ids: set[str] = set()
    for index, value in enumerate(cards):
        path = f"response.result.approval_cards[{index}]"
        card = _mapping(value, path)
        _exact_keys(card, {"card_id", "fingerprint", "title", "kind"}, set(), path)
        card_id = _identifier(card["card_id"], f"{path}.card_id")
        if card_id in card_ids:
            raise BrowserProtocolError("Approval card IDs must be unique")
        card_ids.add(card_id)
        _sha256(card["fingerprint"], f"{path}.fingerprint")
        _bounded_text(card["title"], 512, f"{path}.title")
        _identifier(card["kind"], f"{path}.kind")
    if (result["current_turn_state"] == "approval_required") != bool(cards):
        raise BrowserProtocolError("Approval state contradicts observed approval cards")
    if response_control_id is not None and not any(
        turn["turn_id"] == response_control_id and turn["role"] == "assistant"
        for turn in transcript
    ):
        raise BrowserProtocolError("Response-control identity must name an observed assistant turn")
    if result["root_frame_id"] is None and transcript:
        raise BrowserProtocolError("A rootless chat cannot contain transcript turns")


def _validate_context_observation(result: Mapping[str, Any]) -> None:
    _exact_keys(
        result,
        {"project_id", "enabled_skills", "context_hash"},
        set(),
        "response.result",
    )
    _identifier(result["project_id"], "response.result.project_id")
    skills = _bounded_list(
        result["enabled_skills"], _MAX_ENABLED_SKILLS, "response.result.enabled_skills"
    )
    names: set[str] = set()
    for index, skill in enumerate(skills):
        name = _bounded_text(skill, 256, f"response.result.enabled_skills[{index}]")
        if name in names:
            raise BrowserProtocolError("Enabled skills must be unique")
        names.add(name)
    if list(skills) != sorted(skills):
        raise BrowserProtocolError("Enabled skills must be sorted")
    _sha256(result["context_hash"], "response.result.context_hash")


def _parse_error(value: object, outcome: str) -> BrowserError:
    error = _mapping(value, "response.error")
    _exact_keys(error, _ERROR_KEYS, set(), "response.error")
    _identifier(error["code"], "response.error.code")
    message = error["message"]
    if not isinstance(message, str) or not message or len(message) > 4096:
        raise BrowserProtocolError("response.error.message must be bounded text")
    retryable = error["retryable"]
    if not isinstance(retryable, bool):
        raise BrowserProtocolError("response.error.retryable must be boolean")
    evidence = dict(_mapping(error["evidence"], "response.error.evidence"))
    if len(_compact_json(evidence, "response.error.evidence").encode()) > MAX_ERROR_EVIDENCE_BYTES:
        raise BrowserProtocolError("Response error evidence exceeds the protocol byte limit")
    if outcome == "unknown_outcome" and retryable:
        raise BrowserProtocolError("Unknown outcomes must be non-retryable")
    return BrowserError(
        code=error["code"],
        message=message,
        retryable=retryable,
        evidence=MappingProxyType(evidence),
    )


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise BrowserProtocolError(f"{name} must be a JSON object")
    return value


def _exact_keys(
    value: Mapping[str, Any], required: set[str] | frozenset[str], optional: set[str], name: str
) -> None:
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing or unknown:
        raise BrowserProtocolError(f"{name} has missing or unknown fields")


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise BrowserProtocolError(f"{name} must be a bounded identifier")
    return value


def _nullable_identifier(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, name)


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise BrowserProtocolError(f"{name} must be a SHA-256 digest")
    return value


def _bounded_text(value: object, maximum_bytes: int, name: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > maximum_bytes:
        raise BrowserProtocolError(f"{name} must be bounded text")
    return value


def _bounded_string(value: object, maximum_bytes: int, name: str) -> str:
    if not isinstance(value, str) or len(value.encode()) > maximum_bytes:
        raise BrowserProtocolError(f"{name} must be a bounded string")
    return value


def _bounded_list(value: object, maximum_length: int, name: str) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum_length:
        raise BrowserProtocolError(f"{name} must be a bounded list")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise BrowserProtocolError(f"{name} must be boolean")
    return value


def _bounded_integer(value: object, minimum: int, maximum: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise BrowserProtocolError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _bare_http_origin(value: object) -> None:
    if not isinstance(value, str):
        raise BrowserProtocolError("origin must be a bare HTTP origin")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise BrowserProtocolError("origin must be a bare credential-free HTTP origin") from None
    # Require the input to already be canonical, matching Node's URL.origin (lowercase scheme+host,
    # default port omitted) so Python — the request builder — rejects the same origins Node does,
    # rather than silently passing a non-canonical origin that the Node validator later rejects.
    canonical = f"{parsed.scheme}://{parsed.hostname}"
    if port is not None and port != _DEFAULT_PORTS.get(parsed.scheme):
        canonical += f":{port}"
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or value.endswith("/")
        or value != canonical
    ):
        raise BrowserProtocolError("origin must be a bare credential-free HTTP origin")


def _reject_credential_keys(value: object, path: str) -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_credential_keys(item, f"{path}[{index}]")
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if key.lower() in _CREDENTIAL_KEYS:
            raise BrowserProtocolError(f"{path}.{key} is forbidden in boundary JSON")
        _reject_credential_keys(item, f"{path}.{key}")


def _compact_json(value: object, name: str) -> str:
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise BrowserProtocolError(f"{name} must contain JSON values") from exc
