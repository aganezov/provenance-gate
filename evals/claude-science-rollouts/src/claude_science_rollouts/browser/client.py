"""Typed Python orchestration over the browser subprocess boundary."""

from __future__ import annotations

from dataclasses import dataclass, field

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
