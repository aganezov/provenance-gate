"""Adapt the typed browser boundary into the runtime ``BrowserDriver`` seam.

``TypedBrowserDriver`` wraps one :class:`~claude_science_rollouts.browser.client.BrowserSession`
and speaks the runtime driver protocol. Every method delegates to the boundary and lifts the
boundary result type into its runtime counterpart through a small ``_map_*`` helper. The adapter
owns no orchestration policy, retries nothing, and adds no state beyond the wrapped session.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from claude_science_rollouts.browser import client as boundary

from . import models as runtime

_Boundary = TypeVar("_Boundary")
_Runtime = TypeVar("_Runtime")


def _timing(outcome: Any) -> runtime.Timing:
    return runtime.Timing(
        boundary_elapsed_ms=outcome.boundary_elapsed_ms,
        wall_elapsed_ms=outcome.wall_elapsed_ms,
        transport_overhead_ms=outcome.transport_overhead_ms,
    )


def _error(error: boundary.BrowserError | None) -> runtime.BrowserError | None:
    if error is None:
        return None
    return runtime.BrowserError(error.code, error.message, error.retryable, dict(error.evidence))


def _map_outcome(outcome: Any, result: _Runtime | None) -> runtime.Outcome[_Runtime]:
    """Carry state, timing, and error across the boundary; keep the result only when completed."""
    return runtime.Outcome(
        outcome=outcome.outcome,
        result=result if outcome.outcome == "completed" else None,
        error=_error(outcome.error),
        timing=_timing(outcome),
    )


def _map_observation(
    outcome: boundary.BrowserObservationOutcome[_Boundary],
    lift: Callable[[_Boundary], _Runtime],
) -> runtime.Outcome[_Runtime]:
    result = lift(outcome.observation) if outcome.observation is not None else None
    return _map_outcome(outcome, result)


def _map_operation(
    outcome: boundary.BrowserOperationOutcome[_Boundary],
    lift: Callable[[_Boundary], _Runtime],
) -> runtime.Outcome[_Runtime]:
    result = lift(outcome.result) if outcome.result is not None else None
    return _map_outcome(outcome, result)


def _session(value: boundary.SessionInspection) -> runtime.SessionInspection:
    return runtime.SessionInspection(value.authenticated, value.origin, value.profile_ready)


def _project(value: boundary.ProjectObservation) -> runtime.ProjectObservation:
    return runtime.ProjectObservation(
        value.project_id,
        value.verified,
        value.composer_empty,
        value.user_turn_count,
        value.root_frame_id,
        value.root_state,
    )


def _turn_observation(value: boundary.TurnObservation) -> runtime.TurnObservation:
    return runtime.TurnObservation(value.turn_id, value.role, value.text, value.truncated)


def _approval_card(value: boundary.ApprovalCard) -> runtime.ApprovalCard:
    return runtime.ApprovalCard(value.card_id, value.fingerprint, value.title, value.kind)


def _chat(value: boundary.ChatObservation) -> runtime.ChatObservation:
    return runtime.ChatObservation(
        project_id=value.project_id,
        chat_id=value.chat_id,
        transcript=tuple(_turn_observation(turn) for turn in value.transcript),
        user_turn_count=value.user_turn_count,
        composer_empty=value.composer_empty,
        root_frame_id=value.root_frame_id,
        response_control_id=value.response_control_id,
        current_turn_state=value.current_turn_state,
        approval_cards=tuple(_approval_card(card) for card in value.approval_cards),
    )


def _context(value: boundary.ContextObservation) -> runtime.ContextObservation:
    return runtime.ContextObservation(value.project_id, value.enabled_skills, value.context_hash)


def _attachment(value: boundary.AttachmentAccepted) -> runtime.AttachmentAccepted:
    return runtime.AttachmentAccepted(
        value.project_id, value.chat_id, value.filename, value.accepted
    )


def _model_selection(value: boundary.ModelSelection) -> runtime.ModelSelection:
    return runtime.ModelSelection(
        value.project_id,
        value.chat_id,
        value.model_label,
        value.previous_model_label,
        value.changed,
        value.confirmed,
    )


def _delivery(value: boundary.DeliveryProof) -> runtime.DeliveryProof:
    return runtime.DeliveryProof(
        value.root_frame_id,
        value.authored_prompt_sha256,
        value.delivery_text_sha256,
        value.normalized_user_turn_id,
    )


def _settled(value: boundary.SettledProof) -> runtime.SettledProof:
    return runtime.SettledProof(
        value.stop_hidden,
        value.stable_samples,
        value.new_response_control_id,
    )


def _approval(value: boundary.ApprovalObservation) -> runtime.ApprovalObservation:
    return runtime.ApprovalObservation(tuple(_approval_card(card) for card in value.cards))


def _continuation(value: boundary.TurnContinuation) -> runtime.TurnContinuation:
    return runtime.TurnContinuation(
        value.project_id,
        value.chat_id,
        value.root_frame_id,
        value.authored_prompt_sha256,
        value.delivery_text_sha256,
        value.normalized_user_turn_id,
        value.baseline_response_control_id,
    )


def _turn(value: boundary.TurnResult) -> runtime.TurnResult:
    return runtime.TurnResult(
        project_id=value.project_id,
        chat_id=value.chat_id,
        root_frame_id=value.root_frame_id,
        turn_state=value.turn_state,
        root_created=value.root_created,
        delivery=_delivery(value.delivery) if value.delivery is not None else None,
        settled=_settled(value.settled) if value.settled is not None else None,
        approval=_approval(value.approval) if value.approval is not None else None,
        continuation=(
            _continuation(value.continuation) if value.continuation is not None else None
        ),
    )


def _resolved(value: boundary.ApprovalResolved) -> runtime.ApprovalResolved:
    return runtime.ApprovalResolved(
        value.project_id,
        value.chat_id,
        value.root_frame_id,
        value.card_id,
        value.decision,
        value.verified_cleared,
    )


def _boundary_continuation(value: runtime.TurnContinuation) -> boundary.TurnContinuation:
    """Lower a runtime continuation back to the boundary so a wait resumes the exact same turn."""
    return boundary.TurnContinuation(
        value.project_id,
        value.chat_id,
        value.root_frame_id,
        value.authored_prompt_sha256,
        value.delivery_text_sha256,
        value.normalized_user_turn_id,
        value.baseline_response_control_id,
    )


def _not_started(operation: str) -> runtime.Outcome[Any]:
    """Fail closed for operations the boundary does not implement, without touching it."""
    return runtime.Outcome(
        "not_started",
        None,
        runtime.BrowserError(
            "OPERATION_NOT_IMPLEMENTED",
            f"{operation} is not implemented by the browser boundary",
            False,
            {},
        ),
        runtime.Timing(0, 0, 0),
    )


@dataclass(slots=True)
class TypedBrowserDriver:
    """Satisfy ``BrowserDriver`` over one boundary session without adding policy or retries."""

    session: boundary.BrowserSession

    @property
    def session_id(self) -> str:
        return self.session.client.session_id

    @property
    def origin(self) -> str:
        return self.session.client.origin

    def attach(
        self, *, request_id: str, deadline_ms: int
    ) -> runtime.Outcome[runtime.SessionInspection]:
        outcome = self.session.attach(request_id=request_id, deadline_ms=deadline_ms)
        result = _session(outcome.inspection) if outcome.inspection is not None else None
        return _map_outcome(outcome, result)

    def inspect(
        self, *, request_id: str, deadline_ms: int
    ) -> runtime.Outcome[runtime.SessionInspection]:
        outcome = self.session.inspect(request_id=request_id, deadline_ms=deadline_ms)
        result = _session(outcome.inspection) if outcome.inspection is not None else None
        return _map_outcome(outcome, result)

    def detach(self, *, request_id: str, deadline_ms: int) -> runtime.Outcome[runtime.Detached]:
        if not self.session.attached:
            return _not_started("session detach")
        outcome = self.session.detach(request_id=request_id, deadline_ms=deadline_ms)
        result = runtime.Detached(outcome.detached) if outcome.outcome == "completed" else None
        return _map_outcome(outcome, result)

    def inspect_project(
        self, project_id: str, *, request_id: str, deadline_ms: int
    ) -> runtime.Outcome[runtime.ProjectObservation]:
        return _map_observation(
            self.session.inspect_project(
                project_id, request_id=request_id, deadline_ms=deadline_ms
            ),
            _project,
        )

    def inspect_chat(
        self,
        project_id: str,
        chat_id: str,
        *,
        request_id: str,
        deadline_ms: int,
        root_frame_id: str | None = None,
    ) -> runtime.Outcome[runtime.ChatObservation]:
        return _map_observation(
            self.session.inspect_chat(
                project_id,
                chat_id,
                request_id=request_id,
                deadline_ms=deadline_ms,
                root_frame_id=root_frame_id,
            ),
            _chat,
        )

    def inspect_context(
        self, project_id: str, *, request_id: str, deadline_ms: int
    ) -> runtime.Outcome[runtime.ContextObservation]:
        return _map_observation(
            self.session.inspect_context(
                project_id, request_id=request_id, deadline_ms=deadline_ms
            ),
            _context,
        )

    def submit_turn_wait(
        self,
        project_id: str,
        chat_id: str,
        root_mode: runtime.RootMode,
        prompt: str,
        authored_prompt_sha256: str,
        *,
        request_id: str,
        deadline_ms: int,
        root_frame_id: str | None = None,
    ) -> runtime.Outcome[runtime.TurnResult]:
        return _map_operation(
            self.session.submit_turn_wait(
                project_id,
                chat_id,
                root_mode,
                prompt,
                authored_prompt_sha256,
                request_id=request_id,
                deadline_ms=deadline_ms,
                root_frame_id=root_frame_id,
            ),
            _turn,
        )

    def wait_turn(
        self,
        project_id: str,
        chat_id: str,
        continuation: runtime.TurnContinuation,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> runtime.Outcome[runtime.TurnResult]:
        return _map_operation(
            self.session.wait_turn(
                project_id,
                chat_id,
                _boundary_continuation(continuation),
                request_id=request_id,
                deadline_ms=deadline_ms,
            ),
            _turn,
        )

    def resolve_approval(
        self,
        project_id: str,
        chat_id: str,
        root_frame_id: str,
        card_id: str,
        decision: runtime.ApprovalDecision,
        *,
        request_id: str,
        expected_fingerprint: str,
        deadline_ms: int,
    ) -> runtime.Outcome[runtime.ApprovalResolved]:
        return _map_operation(
            self.session.resolve_approval(
                project_id,
                chat_id,
                root_frame_id,
                card_id,
                decision,
                request_id=request_id,
                expected_fingerprint=expected_fingerprint,
                deadline_ms=deadline_ms,
            ),
            _resolved,
        )

    def create_project(
        self, name: str, *, request_id: str, deadline_ms: int
    ) -> runtime.Outcome[runtime.ProjectObservation]:
        return _map_observation(
            self.session.create_project(name, request_id=request_id, deadline_ms=deadline_ms),
            _project,
        )

    def upload_attachment(
        self,
        project_id: str,
        chat_id: str,
        source_path: str | Path,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> runtime.Outcome[runtime.AttachmentAccepted]:
        return _map_operation(
            self.session.upload_attachment(
                project_id,
                chat_id,
                str(source_path),
                request_id=request_id,
                deadline_ms=deadline_ms,
            ),
            _attachment,
        )

    def select_model(
        self,
        project_id: str,
        chat_id: str,
        model_label: str,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> runtime.Outcome[runtime.ModelSelection]:
        return _map_operation(
            self.session.select_model(
                project_id,
                chat_id,
                model_label,
                request_id=request_id,
                deadline_ms=deadline_ms,
            ),
            _model_selection,
        )

    def update_enabled_skills(
        self,
        project_id: str,
        enabled_skills: frozenset[str],
        *,
        request_id: str,
        expected_before_hash: str,
        deadline_ms: int,
    ) -> runtime.Outcome[runtime.ContextUpdate]:
        return _not_started("agent context update")

    def new_chat(
        self, project_id: str, *, request_id: str, deadline_ms: int
    ) -> runtime.Outcome[runtime.ChatObservation]:
        return _map_observation(
            self.session.new_chat(project_id, request_id=request_id, deadline_ms=deadline_ms),
            _chat,
        )

    def open_chat(
        self, project_id: str, chat_id: str, *, request_id: str, deadline_ms: int
    ) -> runtime.Outcome[runtime.ChatObservation]:
        return _not_started("chat reopening")
