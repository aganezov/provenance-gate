"""Versioned subprocess boundary for browser operations."""

from claude_science_rollouts.browser.bridge import BoundaryInvocation, BrowserBridge
from claude_science_rollouts.browser.client import (
    ApprovalCard,
    BrowserClient,
    BrowserObservationOutcome,
    BrowserSession,
    ChatObservation,
    ContextObservation,
    ProjectObservation,
    SessionDetachOutcome,
    SessionInspection,
    SessionInspectionOutcome,
    TurnObservation,
)
from claude_science_rollouts.browser.errors import (
    BrowserBoundaryError,
    BrowserProcessError,
    BrowserProtocolError,
    BrowserTimeoutError,
)
from claude_science_rollouts.browser.protocol import (
    BrowserError,
    BrowserRequest,
    BrowserResponse,
    make_request,
    parse_response,
)

__all__ = [
    "ApprovalCard",
    "BrowserBoundaryError",
    "BrowserBridge",
    "BrowserClient",
    "BrowserObservationOutcome",
    "BrowserSession",
    "BrowserError",
    "BrowserProcessError",
    "BrowserProtocolError",
    "BrowserRequest",
    "BrowserResponse",
    "BrowserTimeoutError",
    "BoundaryInvocation",
    "ChatObservation",
    "ContextObservation",
    "ProjectObservation",
    "SessionInspection",
    "SessionInspectionOutcome",
    "TurnObservation",
    "SessionDetachOutcome",
    "make_request",
    "parse_response",
]
