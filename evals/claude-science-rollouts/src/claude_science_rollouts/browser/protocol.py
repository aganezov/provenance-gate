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
        "session.inspect",
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
    if operation == "session.inspect" and body:
        raise BrowserProtocolError("session.inspect payload must be empty")
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
        result = MappingProxyType(dict(_mapping(response["result"], "response.result")))
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


def _bounded_integer(value: object, minimum: int, maximum: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise BrowserProtocolError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _bare_http_origin(value: object) -> None:
    if not isinstance(value, str):
        raise BrowserProtocolError("origin must be a bare HTTP origin")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or value.endswith("/")
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
