"""Typed Python orchestration over the browser subprocess boundary."""

from __future__ import annotations

from dataclasses import dataclass

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
class BrowserClient:
    bridge: BrowserBridge
    session_id: str
    origin: str

    def __post_init__(self) -> None:
        if self.bridge.cwd is None or not self.bridge.cwd.is_absolute():
            raise ValueError("browser client requires an explicit absolute working directory")

    def inspect_session(
        self,
        *,
        request_id: str,
        deadline_ms: int = 15_000,
    ) -> SessionInspectionOutcome:
        """Inspect one existing session without opening, navigating, or replaying."""
        request = make_request(
            "session.inspect",
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
