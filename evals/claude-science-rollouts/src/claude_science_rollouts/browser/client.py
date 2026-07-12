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
