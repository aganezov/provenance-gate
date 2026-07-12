"""Typed Python orchestration over the browser subprocess boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar

from claude_science_rollouts.browser.bridge import BrowserBridge
from claude_science_rollouts.browser.protocol import BrowserError, Outcome, make_request


@dataclass(frozen=True)
class SessionInspection:
    authenticated: bool
    origin: str
    profile_ready: bool


@dataclass(frozen=True)
class SessionInspectionOutcome:
    outcome: Outcome
    inspection: SessionInspection | None
    error: BrowserError | None
    boundary_elapsed_ms: int
    wall_elapsed_ms: int

    @property
    def transport_overhead_ms(self) -> int:
        return max(0, self.wall_elapsed_ms - self.boundary_elapsed_ms)


@dataclass(frozen=True)
class SessionDetachOutcome:
    outcome: Outcome
    detached: bool
    error: BrowserError | None
    boundary_elapsed_ms: int
    wall_elapsed_ms: int

    @property
    def transport_overhead_ms(self) -> int:
        return max(0, self.wall_elapsed_ms - self.boundary_elapsed_ms)


Role = Literal["user", "assistant"]
RootMode = Literal["new", "existing"]
ApprovalDecision = Literal["allow_for_conversation", "deny"]
TurnState = Literal[
    "busy",
    "settled",
    "approval_required",
    "input_required",
    "indeterminate",
    "navigation_drift",
    "failed",
]


@dataclass(frozen=True)
class ProjectObservation:
    project_id: str
    verified: bool
    composer_empty: bool
    user_turn_count: int
    root_frame_id: str | None
    root_state: str | None


@dataclass(frozen=True)
class TurnObservation:
    turn_id: str
    role: Role
    text: str
    truncated: bool


@dataclass(frozen=True)
class ApprovalCard:
    card_id: str
    fingerprint: str
    title: str
    kind: str


@dataclass(frozen=True)
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


@dataclass(frozen=True)
class ContextObservation:
    project_id: str
    enabled_skills: frozenset[str]
    context_hash: str


@dataclass(frozen=True)
class AttachmentAccepted:
    project_id: str
    chat_id: str
    filename: str
    accepted: bool


@dataclass(frozen=True)
class DeliveryProof:
    root_frame_id: str
    authored_prompt_sha256: str
    delivery_text_sha256: str
    normalized_user_turn_id: str


@dataclass(frozen=True)
class SettledProof:
    stop_hidden: bool
    stable_samples: int
    new_response_control_id: str


@dataclass(frozen=True)
class TurnContinuation:
    project_id: str
    chat_id: str
    root_frame_id: str
    authored_prompt_sha256: str
    delivery_text_sha256: str
    normalized_user_turn_id: str
    baseline_response_control_id: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "chat_id": self.chat_id,
            "root_frame_id": self.root_frame_id,
            "authored_prompt_sha256": self.authored_prompt_sha256,
            "delivery_text_sha256": self.delivery_text_sha256,
            "normalized_user_turn_id": self.normalized_user_turn_id,
            "baseline_response_control_id": self.baseline_response_control_id,
        }


@dataclass(frozen=True)
class ApprovalObservation:
    cards: tuple[ApprovalCard, ...]


@dataclass(frozen=True)
class TurnResult:
    project_id: str
    chat_id: str
    root_frame_id: str | None
    turn_state: TurnState
    root_created: bool
    delivery: DeliveryProof | None
    settled: SettledProof | None
    approval: ApprovalObservation | None
    continuation: TurnContinuation | None


@dataclass(frozen=True)
class ApprovalResolved:
    project_id: str
    chat_id: str
    root_frame_id: str
    card_id: str
    decision: ApprovalDecision
    verified_cleared: bool


ObservationT = TypeVar("ObservationT")


@dataclass(frozen=True)
class BrowserObservationOutcome(Generic[ObservationT]):
    outcome: Outcome
    observation: ObservationT | None
    error: BrowserError | None
    boundary_elapsed_ms: int
    wall_elapsed_ms: int

    @property
    def transport_overhead_ms(self) -> int:
        return max(0, self.wall_elapsed_ms - self.boundary_elapsed_ms)


ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class BrowserOperationOutcome(Generic[ResultT]):
    outcome: Outcome
    result: ResultT | None
    error: BrowserError | None
    boundary_elapsed_ms: int
    wall_elapsed_ms: int

    @property
    def transport_overhead_ms(self) -> int:
        return max(0, self.wall_elapsed_ms - self.boundary_elapsed_ms)


@dataclass(frozen=True)
class BrowserClient:
    bridge: BrowserBridge
    session_id: str
    origin: str

    def __post_init__(self) -> None:
        if self.bridge.cwd is None or not self.bridge.cwd.is_absolute():
            raise ValueError("browser client requires an explicit absolute working directory")

    def attach_session(
        self,
        *,
        request_id: str,
        deadline_ms: int = 15_000,
    ) -> SessionInspectionOutcome:
        """Attach once to the externally configured browser owner and verify it."""
        return self._inspect_operation(
            "session.attach",
            request_id=request_id,
            deadline_ms=deadline_ms,
        )

    def inspect_session(
        self,
        *,
        request_id: str,
        deadline_ms: int = 15_000,
    ) -> SessionInspectionOutcome:
        """Inspect an attached session without opening, navigating, or replaying."""
        return self._inspect_operation(
            "session.inspect",
            request_id=request_id,
            deadline_ms=deadline_ms,
        )

    def detach_session(
        self,
        *,
        request_id: str,
        deadline_ms: int = 15_000,
    ) -> SessionDetachOutcome:
        """Detach the named browser session without closing its external owner."""
        request = make_request(
            "session.detach",
            request_id=request_id,
            session_id=self.session_id,
            origin=self.origin,
            deadline_ms=deadline_ms,
        )
        invocation = self.bridge.invoke_timed(request)
        response = invocation.response
        detached = False
        if response.completed:
            assert response.result is not None
            detached = response.result["detached"]
        return SessionDetachOutcome(
            outcome=response.outcome,
            detached=detached,
            error=response.error,
            boundary_elapsed_ms=response.elapsed_ms,
            wall_elapsed_ms=invocation.wall_elapsed_ms,
        )

    def inspect_project(
        self,
        project_id: str,
        *,
        request_id: str,
        deadline_ms: int = 15_000,
    ) -> BrowserObservationOutcome[ProjectObservation]:
        """Inspect the active project without navigating or mutating it."""
        return self._observation_operation(
            "project.inspect",
            payload={"project_id": project_id},
            request_id=request_id,
            deadline_ms=deadline_ms,
            parser=_project_observation,
        )

    def inspect_chat(
        self,
        project_id: str,
        chat_id: str,
        *,
        request_id: str,
        root_frame_id: str | None = None,
        deadline_ms: int = 15_000,
    ) -> BrowserObservationOutcome[ChatObservation]:
        """Inspect one active blank or rooted chat with verified identities."""
        payload = {"project_id": project_id, "chat_id": chat_id}
        if root_frame_id is not None:
            payload["root_frame_id"] = root_frame_id
        return self._observation_operation(
            "chat.inspect",
            payload=payload,
            request_id=request_id,
            deadline_ms=deadline_ms,
            parser=_chat_observation,
        )

    def inspect_context(
        self,
        project_id: str,
        *,
        request_id: str,
        deadline_ms: int = 15_000,
    ) -> BrowserObservationOutcome[ContextObservation]:
        """Inspect enabled skills and the normalized context hash."""
        return self._observation_operation(
            "agent_context.inspect",
            payload={"project_id": project_id},
            request_id=request_id,
            deadline_ms=deadline_ms,
            parser=_context_observation,
        )

    def create_project(
        self,
        name: str,
        *,
        request_id: str,
        deadline_ms: int = 20_000,
    ) -> BrowserObservationOutcome[ProjectObservation]:
        """Create one project and verify its fresh rootless state."""
        return self._observation_operation(
            "project.create",
            payload={"name": name},
            request_id=request_id,
            deadline_ms=deadline_ms,
            parser=_project_observation,
        )

    def new_chat(
        self,
        project_id: str,
        *,
        request_id: str,
        deadline_ms: int = 15_000,
    ) -> BrowserObservationOutcome[ChatObservation]:
        """Return a verified blank chat, creating one only when necessary."""
        return self._observation_operation(
            "chat.new",
            payload={"project_id": project_id},
            request_id=request_id,
            deadline_ms=deadline_ms,
            parser=_chat_observation,
        )

    def upload_attachment(
        self,
        project_id: str,
        chat_id: str,
        source_path: str,
        *,
        request_id: str,
        deadline_ms: int = 30_000,
    ) -> BrowserOperationOutcome[AttachmentAccepted]:
        """Upload one local file and return only its accepted basename."""
        return self._typed_operation(
            "attachment.upload",
            payload={
                "project_id": project_id,
                "chat_id": chat_id,
                "source_path": source_path,
            },
            request_id=request_id,
            deadline_ms=deadline_ms,
            parser=_attachment_accepted,
        )

    def submit_turn_wait(
        self,
        project_id: str,
        chat_id: str,
        root_mode: RootMode,
        prompt: str,
        authored_prompt_sha256: str,
        *,
        request_id: str,
        root_frame_id: str | None = None,
        deadline_ms: int = 30_000,
    ) -> BrowserOperationOutcome[TurnResult]:
        """Submit exactly once, prove delivery, and poll locally within one boundary call."""
        payload: dict[str, Any] = {
            "project_id": project_id,
            "chat_id": chat_id,
            "root_mode": root_mode,
            "prompt": prompt,
            "authored_prompt_sha256": authored_prompt_sha256,
        }
        if root_frame_id is not None:
            payload["root_frame_id"] = root_frame_id
        return self._typed_operation(
            "turn.submit_wait",
            payload=payload,
            request_id=request_id,
            deadline_ms=deadline_ms,
            parser=_turn_result,
        )

    def wait_turn(
        self,
        project_id: str,
        chat_id: str,
        continuation: TurnContinuation,
        *,
        request_id: str,
        deadline_ms: int = 30_000,
    ) -> BrowserOperationOutcome[TurnResult]:
        """Resume bounded polling without submitting or replaying a prompt."""
        return self._typed_operation(
            "turn.wait",
            payload={
                "project_id": project_id,
                "chat_id": chat_id,
                "continuation": continuation.to_payload(),
            },
            request_id=request_id,
            deadline_ms=deadline_ms,
            parser=_turn_result,
        )

    def resolve_approval(
        self,
        project_id: str,
        chat_id: str,
        root_frame_id: str,
        card_id: str,
        decision: ApprovalDecision,
        *,
        request_id: str,
        expected_fingerprint: str,
        deadline_ms: int = 15_000,
    ) -> BrowserOperationOutcome[ApprovalResolved]:
        """Resolve one exact scenario-authorized approval card."""
        return self._typed_operation(
            "approval.resolve",
            payload={
                "project_id": project_id,
                "chat_id": chat_id,
                "root_frame_id": root_frame_id,
                "card_id": card_id,
                "decision": decision,
                "expected_fingerprint": expected_fingerprint,
            },
            request_id=request_id,
            deadline_ms=deadline_ms,
            parser=_approval_resolved,
        )

    def _inspect_operation(
        self,
        operation: str,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> SessionInspectionOutcome:
        request = make_request(
            operation,
            request_id=request_id,
            session_id=self.session_id,
            origin=self.origin,
            deadline_ms=deadline_ms,
        )
        invocation = self.bridge.invoke_timed(request)
        response = invocation.response
        inspection = None
        if response.completed:
            assert response.result is not None
            inspection = SessionInspection(
                authenticated=response.result["authenticated"],
                origin=response.result["origin"],
                profile_ready=response.result["profile_ready"],
            )
        return SessionInspectionOutcome(
            outcome=response.outcome,
            inspection=inspection,
            error=response.error,
            boundary_elapsed_ms=response.elapsed_ms,
            wall_elapsed_ms=invocation.wall_elapsed_ms,
        )

    def _observation_operation(
        self,
        operation: str,
        *,
        payload: dict[str, Any],
        request_id: str,
        deadline_ms: int,
        parser: Callable[[Mapping[str, Any]], ObservationT],
    ) -> BrowserObservationOutcome[ObservationT]:
        request = make_request(
            operation,
            request_id=request_id,
            session_id=self.session_id,
            origin=self.origin,
            deadline_ms=deadline_ms,
            payload=payload,
        )
        invocation = self.bridge.invoke_timed(request)
        response = invocation.response
        observation = None
        if response.completed:
            assert response.result is not None
            observation = parser(response.result)
        return BrowserObservationOutcome(
            outcome=response.outcome,
            observation=observation,
            error=response.error,
            boundary_elapsed_ms=response.elapsed_ms,
            wall_elapsed_ms=invocation.wall_elapsed_ms,
        )

    def _typed_operation(
        self,
        operation: str,
        *,
        payload: dict[str, Any],
        request_id: str,
        deadline_ms: int,
        parser: Callable[[Mapping[str, Any]], ResultT],
    ) -> BrowserOperationOutcome[ResultT]:
        request = make_request(
            operation,
            request_id=request_id,
            session_id=self.session_id,
            origin=self.origin,
            deadline_ms=deadline_ms,
            payload=payload,
        )
        invocation = self.bridge.invoke_timed(request)
        response = invocation.response
        result = None
        if response.completed:
            assert response.result is not None
            result = parser(response.result)
        return BrowserOperationOutcome(
            outcome=response.outcome,
            result=result,
            error=response.error,
            boundary_elapsed_ms=response.elapsed_ms,
            wall_elapsed_ms=invocation.wall_elapsed_ms,
        )


@dataclass
class BrowserSession:
    """Python-owned state machine for one attach-many-detach lifecycle."""

    client: BrowserClient
    _attached: bool = field(default=False, init=False)

    @property
    def attached(self) -> bool:
        return self._attached

    def attach(
        self,
        *,
        request_id: str,
        deadline_ms: int = 15_000,
    ) -> SessionInspectionOutcome:
        if self._attached:
            raise RuntimeError("browser session is already attached")
        outcome = self.client.attach_session(
            request_id=request_id,
            deadline_ms=deadline_ms,
        )
        if outcome.outcome == "completed":
            self._attached = True
        return outcome

    def inspect(
        self,
        *,
        request_id: str,
        deadline_ms: int = 15_000,
    ) -> SessionInspectionOutcome:
        if not self._attached:
            raise RuntimeError("browser session is not attached")
        return self.client.inspect_session(
            request_id=request_id,
            deadline_ms=deadline_ms,
        )

    def detach(
        self,
        *,
        request_id: str,
        deadline_ms: int = 15_000,
    ) -> SessionDetachOutcome:
        if not self._attached:
            raise RuntimeError("browser session is not attached")
        outcome = self.client.detach_session(
            request_id=request_id,
            deadline_ms=deadline_ms,
        )
        if outcome.outcome == "completed" and outcome.detached:
            self._attached = False
        return outcome

    def inspect_project(
        self,
        project_id: str,
        *,
        request_id: str,
        deadline_ms: int = 15_000,
    ) -> BrowserObservationOutcome[ProjectObservation]:
        self._require_attached()
        return self.client.inspect_project(
            project_id,
            request_id=request_id,
            deadline_ms=deadline_ms,
        )

    def inspect_chat(
        self,
        project_id: str,
        chat_id: str,
        *,
        request_id: str,
        root_frame_id: str | None = None,
        deadline_ms: int = 15_000,
    ) -> BrowserObservationOutcome[ChatObservation]:
        self._require_attached()
        return self.client.inspect_chat(
            project_id,
            chat_id,
            request_id=request_id,
            root_frame_id=root_frame_id,
            deadline_ms=deadline_ms,
        )

    def inspect_context(
        self,
        project_id: str,
        *,
        request_id: str,
        deadline_ms: int = 15_000,
    ) -> BrowserObservationOutcome[ContextObservation]:
        self._require_attached()
        return self.client.inspect_context(
            project_id,
            request_id=request_id,
            deadline_ms=deadline_ms,
        )

    def create_project(
        self,
        name: str,
        *,
        request_id: str,
        deadline_ms: int = 20_000,
    ) -> BrowserObservationOutcome[ProjectObservation]:
        self._require_attached()
        return self.client.create_project(
            name,
            request_id=request_id,
            deadline_ms=deadline_ms,
        )

    def new_chat(
        self,
        project_id: str,
        *,
        request_id: str,
        deadline_ms: int = 15_000,
    ) -> BrowserObservationOutcome[ChatObservation]:
        self._require_attached()
        return self.client.new_chat(
            project_id,
            request_id=request_id,
            deadline_ms=deadline_ms,
        )

    def upload_attachment(
        self,
        project_id: str,
        chat_id: str,
        source_path: str,
        *,
        request_id: str,
        deadline_ms: int = 30_000,
    ) -> BrowserOperationOutcome[AttachmentAccepted]:
        self._require_attached()
        return self.client.upload_attachment(
            project_id,
            chat_id,
            source_path,
            request_id=request_id,
            deadline_ms=deadline_ms,
        )

    def submit_turn_wait(
        self,
        project_id: str,
        chat_id: str,
        root_mode: RootMode,
        prompt: str,
        authored_prompt_sha256: str,
        *,
        request_id: str,
        root_frame_id: str | None = None,
        deadline_ms: int = 30_000,
    ) -> BrowserOperationOutcome[TurnResult]:
        self._require_attached()
        return self.client.submit_turn_wait(
            project_id,
            chat_id,
            root_mode,
            prompt,
            authored_prompt_sha256,
            request_id=request_id,
            root_frame_id=root_frame_id,
            deadline_ms=deadline_ms,
        )

    def wait_turn(
        self,
        project_id: str,
        chat_id: str,
        continuation: TurnContinuation,
        *,
        request_id: str,
        deadline_ms: int = 30_000,
    ) -> BrowserOperationOutcome[TurnResult]:
        self._require_attached()
        return self.client.wait_turn(
            project_id,
            chat_id,
            continuation,
            request_id=request_id,
            deadline_ms=deadline_ms,
        )

    def resolve_approval(
        self,
        project_id: str,
        chat_id: str,
        root_frame_id: str,
        card_id: str,
        decision: ApprovalDecision,
        *,
        request_id: str,
        expected_fingerprint: str,
        deadline_ms: int = 15_000,
    ) -> BrowserOperationOutcome[ApprovalResolved]:
        self._require_attached()
        return self.client.resolve_approval(
            project_id,
            chat_id,
            root_frame_id,
            card_id,
            decision,
            request_id=request_id,
            expected_fingerprint=expected_fingerprint,
            deadline_ms=deadline_ms,
        )

    def _require_attached(self) -> None:
        if not self._attached:
            raise RuntimeError("browser session is not attached")


def _project_observation(result: Mapping[str, Any]) -> ProjectObservation:
    return ProjectObservation(
        project_id=result["project_id"],
        verified=result["verified"],
        composer_empty=result["composer_empty"],
        user_turn_count=result["user_turn_count"],
        root_frame_id=result["root_frame_id"],
        root_state=result["root_state"],
    )


def _chat_observation(result: Mapping[str, Any]) -> ChatObservation:
    transcript = tuple(
        TurnObservation(
            turn_id=turn["turn_id"],
            role=turn["role"],
            text=turn["text"],
            truncated=turn["truncated"],
        )
        for turn in result["transcript"]
    )
    cards = tuple(
        ApprovalCard(
            card_id=card["card_id"],
            fingerprint=card["fingerprint"],
            title=card["title"],
            kind=card["kind"],
        )
        for card in result["approval_cards"]
    )
    return ChatObservation(
        project_id=result["project_id"],
        chat_id=result["chat_id"],
        transcript=transcript,
        user_turn_count=result["user_turn_count"],
        composer_empty=result["composer_empty"],
        root_frame_id=result["root_frame_id"],
        response_control_id=result["response_control_id"],
        current_turn_state=result["current_turn_state"],
        approval_cards=cards,
    )


def _context_observation(result: Mapping[str, Any]) -> ContextObservation:
    return ContextObservation(
        project_id=result["project_id"],
        enabled_skills=frozenset(result["enabled_skills"]),
        context_hash=result["context_hash"],
    )


def _attachment_accepted(result: Mapping[str, Any]) -> AttachmentAccepted:
    return AttachmentAccepted(
        project_id=result["project_id"],
        chat_id=result["chat_id"],
        filename=result["filename"],
        accepted=result["accepted"],
    )


def _approval_card(value: Mapping[str, Any]) -> ApprovalCard:
    return ApprovalCard(
        card_id=value["card_id"],
        fingerprint=value["fingerprint"],
        title=value["title"],
        kind=value["kind"],
    )


def _delivery_proof(value: Mapping[str, Any]) -> DeliveryProof:
    return DeliveryProof(
        root_frame_id=value["root_frame_id"],
        authored_prompt_sha256=value["authored_prompt_sha256"],
        delivery_text_sha256=value["delivery_text_sha256"],
        normalized_user_turn_id=value["normalized_user_turn_id"],
    )


def _turn_continuation(value: Mapping[str, Any]) -> TurnContinuation:
    return TurnContinuation(
        project_id=value["project_id"],
        chat_id=value["chat_id"],
        root_frame_id=value["root_frame_id"],
        authored_prompt_sha256=value["authored_prompt_sha256"],
        delivery_text_sha256=value["delivery_text_sha256"],
        normalized_user_turn_id=value["normalized_user_turn_id"],
        baseline_response_control_id=value["baseline_response_control_id"],
    )


def _turn_result(result: Mapping[str, Any]) -> TurnResult:
    delivery = result["delivery"]
    settled = result["settled"]
    approval = result["approval"]
    continuation = result["continuation"]
    return TurnResult(
        project_id=result["project_id"],
        chat_id=result["chat_id"],
        root_frame_id=result["root_frame_id"],
        turn_state=result["turn_state"],
        root_created=result["root_created"],
        delivery=_delivery_proof(delivery) if delivery is not None else None,
        settled=(
            SettledProof(
                stop_hidden=settled["stop_hidden"],
                stable_samples=settled["stable_samples"],
                new_response_control_id=settled["new_response_control_id"],
            )
            if settled is not None
            else None
        ),
        approval=(
            ApprovalObservation(cards=tuple(_approval_card(card) for card in approval["cards"]))
            if approval is not None
            else None
        ),
        continuation=(_turn_continuation(continuation) if continuation is not None else None),
    )


def _approval_resolved(result: Mapping[str, Any]) -> ApprovalResolved:
    return ApprovalResolved(
        project_id=result["project_id"],
        chat_id=result["chat_id"],
        root_frame_id=result["root_frame_id"],
        card_id=result["card_id"],
        decision=result["decision"],
        verified_cleared=result["verified_cleared"],
    )
