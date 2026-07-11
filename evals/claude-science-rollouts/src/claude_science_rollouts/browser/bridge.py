"""One-shot subprocess bridge to the narrow browser boundary."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from claude_science_rollouts.browser.errors import (
    BrowserProcessError,
    BrowserProtocolError,
    BrowserTimeoutError,
)
from claude_science_rollouts.browser.protocol import (
    MAX_RESPONSE_BYTES,
    BrowserRequest,
    BrowserResponse,
    parse_response,
)

MAX_STDERR_BYTES = 16_384


@dataclass(frozen=True)
class BrowserBridge:
    """Invoke exactly one boundary process per request, with no implicit replay."""

    command: tuple[str, ...]
    cwd: Path | None = None
    timeout_headroom_ms: int = 1_000

    def __post_init__(self) -> None:
        if not self.command or any(not item for item in self.command):
            raise ValueError("command must contain non-empty arguments")
        if self.timeout_headroom_ms < 0:
            raise ValueError("timeout_headroom_ms cannot be negative")

    def invoke(self, request: BrowserRequest) -> BrowserResponse:
        """Run once and return a correlated response, including non-completed outcomes."""
        timeout_seconds = (request.deadline_ms + self.timeout_headroom_ms) / 1000
        try:
            process = subprocess.run(
                self.command,
                input=request.to_json(),
                capture_output=True,
                text=True,
                cwd=self.cwd,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BrowserTimeoutError(
                f"Browser boundary exceeded its {request.deadline_ms} ms deadline"
            ) from exc
        except OSError as exc:
            raise BrowserProcessError(returncode=-1, stderr=_bounded_text(str(exc))) from exc

        stderr = _bounded_text(process.stderr)
        if process.returncode != 0:
            raise BrowserProcessError(returncode=process.returncode, stderr=stderr)
        if process.stderr:
            raise BrowserProtocolError("Successful boundary process wrote to stderr")
        if len(process.stdout.encode()) > MAX_RESPONSE_BYTES:
            raise BrowserProtocolError("Response exceeds the protocol byte limit")
        return parse_response(process.stdout, request)


def _bounded_text(value: str) -> str:
    encoded = value.encode()[:MAX_STDERR_BYTES]
    return encoded.decode(errors="replace")
