"""Failure types for the browser subprocess boundary."""

from __future__ import annotations

from dataclasses import dataclass


class BrowserBoundaryError(RuntimeError):
    """Base class for failures before a valid boundary response is available."""


class BrowserProtocolError(BrowserBoundaryError):
    """The request or response violated the versioned JSON protocol."""


class BrowserTimeoutError(BrowserBoundaryError):
    """The boundary process exceeded the caller-owned deadline."""


@dataclass(frozen=True)
class BrowserProcessError(BrowserBoundaryError):
    """The boundary process failed without returning a valid response."""

    returncode: int
    stderr: str

    def __str__(self) -> str:
        detail = f": {self.stderr}" if self.stderr else ""
        return f"Browser boundary exited with status {self.returncode}{detail}"
