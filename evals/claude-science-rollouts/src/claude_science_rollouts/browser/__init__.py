"""Versioned subprocess boundary for browser operations."""

from claude_science_rollouts.browser.bridge import BrowserBridge
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
    "BrowserError",
    "BrowserProcessError",
    "BrowserProtocolError",
    "BrowserRequest",
    "BrowserResponse",
    "BrowserTimeoutError",
    "make_request",
    "parse_response",
]
