"""Turn orchestration: submit once, resolve exact approvals, resume without replay."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .driver import BrowserDriver
from .models import (
    ApprovalDecision,
    ApprovalResolved,
    Outcome,
    RootMode,
    TurnResult,
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,139}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
TurnStopReason = Literal["settled", "terminal_observation", "non_completed", "policy_exceeded"]
_TERMINAL_STATES = frozenset(
    {"input_required", "indeterminate", "navigation_drift", "failed"}
)


@dataclass(frozen=True, slots=True)
class TurnApprovalPolicy:
    action: ApprovalDecision = "deny"
    max_approvals: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.max_approvals, bool) or not isinstance(self.max_approvals, int):
            raise ValueError("max_approvals must be an integer")
        if self.action == "deny":
            if self.max_approvals != 0:
                raise ValueError("deny policy requires max_approvals=0")
        elif self.action == "allow_for_conversation":
            if not 1 <= self.max_approvals <= 32:
                raise ValueError("allow policy requires max_approvals in 1..32")
        else:
            raise ValueError("unsupported approval policy")


@dataclass(slots=True)
class TurnApprovalBudget:
    action: ApprovalDecision
    remaining: int

    @classmethod
    def from_policy(cls, policy: TurnApprovalPolicy) -> TurnApprovalBudget:
        return cls(policy.action, policy.max_approvals)

    def take_decision(self) -> tuple[ApprovalDecision, bool]:
        if self.action == "allow_for_conversation" and self.remaining > 0:
            self.remaining -= 1
            return "allow_for_conversation", True
        return "deny", False


@dataclass(frozen=True, slots=True)
class TurnRequest:
    project_id: str
    chat_id: str
    root_mode: RootMode
    prompt: str
    authored_prompt_sha256: str
    request_id_prefix: str
    deadline_ms: int
    root_frame_id: str | None = None
    max_waits: int = 32

    def __post_init__(self) -> None:
        for name in ("project_id", "chat_id", "request_id_prefix"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{name} must be a bounded identifier")
        if self.root_mode not in {"new", "existing"}:
            raise ValueError("unsupported root mode")
        if self.root_mode == "new" and self.root_frame_id is not None:
            raise ValueError("new root mode cannot provide root_frame_id")
        if self.root_mode == "existing" and self.root_frame_id is None:
            raise ValueError("existing root mode requires root_frame_id")
        if not isinstance(self.prompt, str) or not self.prompt:
            raise ValueError("prompt must be non-empty text")
        if not _SHA256.fullmatch(self.authored_prompt_sha256):
            raise ValueError("authored_prompt_sha256 must be 64 lowercase hex characters")
        if (
            isinstance(self.deadline_ms, bool)
            or not isinstance(self.deadline_ms, int)
            or self.deadline_ms <= 0
        ):
            raise ValueError("deadline_ms must be a positive integer")
        if (
            isinstance(self.max_waits, bool)
            or not isinstance(self.max_waits, int)
            or not 1 <= self.max_waits <= 128
        ):
            raise ValueError("max_waits must be in 1..128")


@dataclass(frozen=True, slots=True)
class TurnExecution:
    final: Outcome[TurnResult]
    approval_resolutions: tuple[Outcome[ApprovalResolved], ...]
    wait_count: int
    stop_reason: TurnStopReason


class TurnLimitError(RuntimeError):
    """Raised when a delivered turn does not settle within the caller-owned wait bound."""


class TurnProtocolError(RuntimeError):
    """Raised when a completed driver result violates its requested identities."""


def _as_turn_outcome(outcome: Outcome[ApprovalResolved]) -> Outcome[TurnResult]:
    if outcome.completed:
        raise ValueError("completed approval outcome cannot replace a turn outcome")
    return Outcome(
        outcome=outcome.outcome,
        result=None,
        error=outcome.error,
        timing=outcome.timing,
    )


def _validate_turn_identity(
    turn: TurnResult,
    request: TurnRequest,
    expected_root_frame_id: str | None,
    *,
    first_result: bool,
) -> str:
    if turn.project_id != request.project_id or turn.chat_id != request.chat_id:
        raise TurnProtocolError("turn result project/chat identity mismatch")
    if expected_root_frame_id is not None and turn.root_frame_id != expected_root_frame_id:
        raise TurnProtocolError("turn result root identity mismatch")
    if first_result and turn.root_created != (request.root_mode == "new"):
        raise TurnProtocolError("turn result root_created does not match root mode")
    for proof in (turn.delivery, turn.continuation):
        if proof is not None and proof.authored_prompt_sha256 != request.authored_prompt_sha256:
            raise TurnProtocolError("turn result authored prompt identity mismatch")
    return turn.root_frame_id


def _validate_approval_echo(
    resolved: ApprovalResolved,
    turn: TurnResult,
    card_id: str,
    decision: ApprovalDecision,
) -> None:
    if (
        resolved.project_id != turn.project_id
        or resolved.chat_id != turn.chat_id
        or resolved.root_frame_id != turn.root_frame_id
        or resolved.card_id != card_id
        or resolved.decision != decision
    ):
        raise TurnProtocolError("approval result identity or decision mismatch")


def run_turn(
    driver: BrowserDriver,
    request: TurnRequest,
    *,
    approval_policy: TurnApprovalPolicy | None = None,
    approval_budget: TurnApprovalBudget | None = None,
) -> TurnExecution:
    """Drive one turn to a bounded terminal observation without replaying its prompt."""
    policy = approval_policy or TurnApprovalPolicy()
    budget = approval_budget or TurnApprovalBudget.from_policy(policy)
    current = driver.submit_turn_wait(
        request.project_id,
        request.chat_id,
        request.root_mode,
        request.prompt,
        request.authored_prompt_sha256,
        request_id=f"{request.request_id_prefix}.submit",
        deadline_ms=request.deadline_ms,
        root_frame_id=request.root_frame_id,
    )
    resolutions: list[Outcome[ApprovalResolved]] = []
    waits = 0
    expected_root_frame_id = request.root_frame_id
    first_result = True

    while True:
        if not current.completed:
            return TurnExecution(current, tuple(resolutions), waits, "non_completed")
        turn = current.result
        assert turn is not None
        expected_root_frame_id = _validate_turn_identity(
            turn,
            request,
            expected_root_frame_id,
            first_result=first_result,
        )
        first_result = False
        if turn.turn_state == "settled":
            return TurnExecution(current, tuple(resolutions), waits, "settled")
        if turn.turn_state in _TERMINAL_STATES:
            return TurnExecution(current, tuple(resolutions), waits, "terminal_observation")
        if turn.continuation is None:
            raise TurnProtocolError("busy turn must be delivered with a continuation")
        if waits >= request.max_waits:
            raise TurnLimitError(f"turn did not settle within {request.max_waits} waits")

        if turn.turn_state == "approval_required":
            assert turn.approval is not None
            if len(turn.approval.cards) != 1:
                raise TurnProtocolError(
                    "approval_required must contain exactly one actionable card"
                )
            card = turn.approval.cards[0]
            decision, within_budget = budget.take_decision()
            resolution = driver.resolve_approval(
                turn.project_id,
                turn.chat_id,
                turn.root_frame_id,
                card.card_id,
                decision,
                request_id=f"{request.request_id_prefix}.approval.{len(resolutions) + 1}",
                expected_fingerprint=card.fingerprint,
                deadline_ms=request.deadline_ms,
            )
            resolutions.append(resolution)
            if not resolution.completed:
                return TurnExecution(
                    _as_turn_outcome(resolution),
                    tuple(resolutions),
                    waits,
                    "non_completed",
                )
            assert resolution.result is not None
            _validate_approval_echo(resolution.result, turn, card.card_id, decision)
            if not within_budget:
                return TurnExecution(current, tuple(resolutions), waits, "policy_exceeded")

        waits += 1
        current = driver.wait_turn(
            turn.project_id,
            turn.chat_id,
            turn.continuation,
            request_id=f"{request.request_id_prefix}.wait.{waits}",
            deadline_ms=request.deadline_ms,
        )
