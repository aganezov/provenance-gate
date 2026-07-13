"""Frozen runtime DTOs for the browser-driver seam."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

Role = Literal["user", "assistant"]
ApprovalDecision = Literal["allow_for_conversation", "deny"]
OutcomeState = Literal["not_started", "completed", "unknown_outcome"]
TurnState = Literal[
    "busy",
    "settled",
    "approval_required",
    "input_required",
    "indeterminate",
    "navigation_drift",
    "failed",
]
RootMode = Literal["new", "existing"]

T = TypeVar("T")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OUTCOME_STATES = frozenset({"not_started", "completed", "unknown_outcome"})
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


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a bounded identifier")


def _optional_identifier(value: str | None, name: str) -> None:
    if value is not None:
        _identifier(value, name)


def _authored_sha256(value: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError("authored_prompt_sha256 must be 64 lowercase hex characters")


def _boolean(value: object, name: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean")


def _non_negative_integer(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _model_label(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value.encode()) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{name} must be a bounded model label")


@dataclass(frozen=True, slots=True)
class ModelSelection:
    """The model a chat is set to, and whether selecting it changed and confirmed the setting."""

    project_id: str
    chat_id: str
    model_label: str
    previous_model_label: str
    changed: bool
    confirmed: bool

    def __post_init__(self) -> None:
        _identifier(self.project_id, "project_id")
        _identifier(self.chat_id, "chat_id")
        _model_label(self.model_label, "model_label")
        _model_label(self.previous_model_label, "previous_model_label")
        _boolean(self.changed, "changed")
        _boolean(self.confirmed, "confirmed")
        if self.changed != (self.previous_model_label != self.model_label):
            raise ValueError("changed must reflect whether the model label changed")


@dataclass(frozen=True, slots=True)
class Timing:
    boundary_elapsed_ms: int
    wall_elapsed_ms: int
    transport_overhead_ms: int

    def __post_init__(self) -> None:
        values = (self.boundary_elapsed_ms, self.wall_elapsed_ms, self.transport_overhead_ms)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ):
            raise ValueError("timing values must be non-negative integers")


@dataclass(frozen=True, slots=True)
class BrowserError:
    code: str
    message: str
    retryable: bool
    evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        _identifier(self.code, "error.code")
        if not isinstance(self.message, str) or not self.message or len(self.message) > 4096:
            raise ValueError("error.message must be bounded non-empty text")
        if not isinstance(self.retryable, bool):
            raise ValueError("error.retryable must be boolean")
        try:
            encoded = json.dumps(self.evidence, separators=(",", ":"), ensure_ascii=False).encode()
        except (TypeError, ValueError) as exc:
            raise ValueError("error.evidence must contain JSON values") from exc
        if len(encoded) > 8192:
            raise ValueError("error.evidence exceeds 8192 bytes")


@dataclass(frozen=True, slots=True)
class Outcome(Generic[T]):
    outcome: OutcomeState
    result: T | None
    error: BrowserError | None
    timing: Timing

    def __post_init__(self) -> None:
        if self.outcome not in _OUTCOME_STATES:
            raise ValueError("unsupported outcome")
        if self.outcome == "completed":
            if self.result is None or self.error is not None:
                raise ValueError("completed outcome requires only a result")
        elif self.result is not None or self.error is None:
            raise ValueError("non-completed outcome requires only an error")
        if self.outcome == "unknown_outcome" and self.error and self.error.retryable:
            raise ValueError("unknown outcomes cannot be retryable")

    @property
    def completed(self) -> bool:
        return self.outcome == "completed"


@dataclass(frozen=True, slots=True)
class SessionInspection:
    authenticated: bool
    origin: str
    profile_ready: bool

    def __post_init__(self) -> None:
        _boolean(self.authenticated, "authenticated")
        _boolean(self.profile_ready, "profile_ready")
        if not isinstance(self.origin, str) or not self.origin:
            raise ValueError("origin must be non-empty text")


@dataclass(frozen=True, slots=True)
class Detached:
    detached: bool

    def __post_init__(self) -> None:
        _boolean(self.detached, "detached")


@dataclass(frozen=True, slots=True)
class ProjectObservation:
    project_id: str
    verified: bool
    composer_empty: bool
    user_turn_count: int
    root_frame_id: str | None
    root_state: str | None

    def __post_init__(self) -> None:
        _identifier(self.project_id, "project_id")
        _optional_identifier(self.root_frame_id, "root_frame_id")
        _boolean(self.verified, "verified")
        _boolean(self.composer_empty, "composer_empty")
        _non_negative_integer(self.user_turn_count, "user_turn_count")
        if self.root_state is not None and not isinstance(self.root_state, str):
            raise ValueError("root_state must be text or None")


@dataclass(frozen=True, slots=True)
class TurnObservation:
    turn_id: str
    role: Role
    text: str
    truncated: bool

    def __post_init__(self) -> None:
        _identifier(self.turn_id, "turn_id")
        if self.role not in {"user", "assistant"}:
            raise ValueError("unsupported turn role")
        if not isinstance(self.text, str) or len(self.text.encode()) > 16384:
            raise ValueError("turn text exceeds 16384 bytes")
        _boolean(self.truncated, "truncated")


@dataclass(frozen=True, slots=True)
class ApprovalCard:
    card_id: str
    fingerprint: str
    title: str
    kind: str

    def __post_init__(self) -> None:
        _identifier(self.card_id, "card_id")
        fields = (self.fingerprint, self.title, self.kind)
        if not all(isinstance(value, str) and value for value in fields):
            raise ValueError("approval-card fields must be non-empty text")


@dataclass(frozen=True, slots=True)
class ChatObservation:
    project_id: str
    chat_id: str
    transcript: tuple[TurnObservation, ...]
    user_turn_count: int
    composer_empty: bool
    root_frame_id: str | None
    response_control_id: str | None
    current_turn_state: TurnState
    approval_cards: tuple[ApprovalCard, ...]

    def __post_init__(self) -> None:
        _identifier(self.project_id, "project_id")
        _identifier(self.chat_id, "chat_id")
        _optional_identifier(self.root_frame_id, "root_frame_id")
        _optional_identifier(self.response_control_id, "response_control_id")
        _non_negative_integer(self.user_turn_count, "user_turn_count")
        _boolean(self.composer_empty, "composer_empty")
        if self.current_turn_state not in _TURN_STATES:
            raise ValueError("unsupported turn state")


@dataclass(frozen=True, slots=True)
class ContextObservation:
    project_id: str
    enabled_skills: frozenset[str]
    context_hash: str

    def __post_init__(self) -> None:
        _identifier(self.project_id, "project_id")
        if not isinstance(self.enabled_skills, frozenset):
            raise ValueError("enabled_skills must be a frozenset")
        if not isinstance(self.context_hash, str) or not self.context_hash:
            raise ValueError("context_hash must be non-empty text")


@dataclass(frozen=True, slots=True)
class ContextUpdate:
    project_id: str
    before_hash: str
    after_hash: str

    def __post_init__(self) -> None:
        _identifier(self.project_id, "project_id")
        hashes = (self.before_hash, self.after_hash)
        if not all(isinstance(value, str) and value for value in hashes):
            raise ValueError("context hashes must be non-empty text")


@dataclass(frozen=True, slots=True)
class AttachmentAccepted:
    project_id: str
    chat_id: str
    filename: str
    accepted: bool

    def __post_init__(self) -> None:
        _identifier(self.project_id, "project_id")
        _identifier(self.chat_id, "chat_id")
        if not isinstance(self.filename, str) or not self.filename:
            raise ValueError("filename must be non-empty text")
        _boolean(self.accepted, "accepted")


@dataclass(frozen=True, slots=True)
class ApprovalResolved:
    project_id: str
    chat_id: str
    root_frame_id: str
    card_id: str
    decision: ApprovalDecision
    verified_cleared: bool

    def __post_init__(self) -> None:
        for name in ("project_id", "chat_id", "root_frame_id", "card_id"):
            _identifier(getattr(self, name), name)
        if self.decision not in {"allow_for_conversation", "deny"}:
            raise ValueError("unsupported approval decision")
        if self.verified_cleared is not True:
            raise ValueError("completed approval resolution must be verified cleared")


@dataclass(frozen=True, slots=True)
class TurnContinuation:
    project_id: str
    chat_id: str
    root_frame_id: str
    authored_prompt_sha256: str
    delivery_text_sha256: str
    normalized_user_turn_id: str
    baseline_response_control_id: str | None

    def __post_init__(self) -> None:
        for name in ("project_id", "chat_id", "root_frame_id", "normalized_user_turn_id"):
            _identifier(getattr(self, name), name)
        _optional_identifier(self.baseline_response_control_id, "baseline_response_control_id")
        _authored_sha256(self.authored_prompt_sha256)
        if not isinstance(self.delivery_text_sha256, str):
            raise ValueError("delivery_text_sha256 must be opaque text")


@dataclass(frozen=True, slots=True)
class DeliveryProof:
    root_frame_id: str
    authored_prompt_sha256: str
    delivery_text_sha256: str
    normalized_user_turn_id: str

    def __post_init__(self) -> None:
        _identifier(self.root_frame_id, "root_frame_id")
        _identifier(self.normalized_user_turn_id, "normalized_user_turn_id")
        _authored_sha256(self.authored_prompt_sha256)
        if not isinstance(self.delivery_text_sha256, str):
            raise ValueError("delivery_text_sha256 must be opaque text")


@dataclass(frozen=True, slots=True)
class SettledProof:
    stop_hidden: bool
    stable_samples: int
    new_response_control_id: str

    def __post_init__(self) -> None:
        _boolean(self.stop_hidden, "stop_hidden")
        if isinstance(self.stable_samples, bool) or self.stable_samples <= 0:
            raise ValueError("stable_samples must be a positive integer")
        _identifier(self.new_response_control_id, "new_response_control_id")


@dataclass(frozen=True, slots=True)
class ApprovalObservation:
    cards: tuple[ApprovalCard, ...]


@dataclass(frozen=True, slots=True)
class TurnResult:
    project_id: str
    chat_id: str
    root_frame_id: str
    turn_state: TurnState
    root_created: bool
    delivery: DeliveryProof | None
    settled: SettledProof | None
    approval: ApprovalObservation | None
    continuation: TurnContinuation | None

    def __post_init__(self) -> None:
        for name in ("project_id", "chat_id", "root_frame_id"):
            _identifier(getattr(self, name), name)
        _boolean(self.root_created, "root_created")
        if self.turn_state not in _TURN_STATES:
            raise ValueError("unsupported turn state")
        if (self.turn_state == "approval_required") != (self.approval is not None):
            raise ValueError("approval is present exactly for approval_required")
        if self.turn_state == "settled":
            if self.delivery is None or self.settled is None:
                raise ValueError("settled result requires delivery and settled proofs")
        elif self.settled is not None:
            raise ValueError("settled proof is valid only for settled state")
        needs_continuation = self.delivery is not None and self.settled is None
        if (self.continuation is not None) != needs_continuation:
            raise ValueError("continuation is present exactly when delivered but not settled")
        if self.turn_state == "approval_required" and self.continuation is None:
            raise ValueError("approval_required result must be delivered with a continuation")
        identities = [self.delivery, self.continuation]
        for proof in identities:
            if proof is not None and proof.root_frame_id != self.root_frame_id:
                raise ValueError("turn proof root identity does not match result")
        if self.continuation is not None and (
            self.continuation.project_id != self.project_id
            or self.continuation.chat_id != self.chat_id
        ):
            raise ValueError("continuation identity does not match result")
        if self.delivery is not None and self.continuation is not None:
            if (
                self.delivery.authored_prompt_sha256
                != self.continuation.authored_prompt_sha256
                or self.delivery.delivery_text_sha256
                != self.continuation.delivery_text_sha256
                or self.delivery.normalized_user_turn_id
                != self.continuation.normalized_user_turn_id
            ):
                raise ValueError("delivery and continuation prompt identities do not match")
