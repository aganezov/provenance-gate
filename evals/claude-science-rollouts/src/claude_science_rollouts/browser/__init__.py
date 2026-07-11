"""Versioned subprocess boundary for browser operations."""

from claude_science_rollouts.browser.bridge import BoundaryInvocation, BrowserBridge
from claude_science_rollouts.browser.client import (
    BrowserClient,
    SessionInspection,
    SessionInspectionOutcome,
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
    "BrowserBoundaryError",
    "BrowserBridge",
    "BrowserClient",
    "BrowserError",
    "BrowserProcessError",
    "BrowserProtocolError",
    "BrowserRequest",
    "BrowserResponse",
    "BrowserTimeoutError",
    "BoundaryInvocation",
    "SessionInspection",
    "SessionInspectionOutcome",
    "make_request",
    "parse_response",
]
