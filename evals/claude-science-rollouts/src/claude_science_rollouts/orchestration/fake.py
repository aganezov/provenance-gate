"""Scripted fake driver for deterministic orchestration tests."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .models import (
    ApprovalDecision,
    ApprovalResolved,
    AttachmentAccepted,
    ChatObservation,
    ContextObservation,
    ContextUpdate,
    Detached,
    Outcome,
    ProjectObservation,
    RootMode,
    SessionInspection,
    TurnContinuation,
    TurnResult,
)


@dataclass(frozen=True, slots=True)
class DriverCall:
    operation: str
    arguments: tuple[object, ...]
    keywords: Mapping[str, object]


class FakeBrowserDriver:
    """Return scripted outcomes in order while recording every boundary call."""

    def __init__(
        self,
        session_id: str,
        origin: str,
        scripts: Mapping[str, Iterable[Outcome[Any]]],
    ) -> None:
        self.session_id = session_id
        self.origin = origin
        self._scripts = {operation: deque(outcomes) for operation, outcomes in scripts.items()}
        self._calls: list[DriverCall] = []

    @property
    def calls(self) -> tuple[DriverCall, ...]:
        return tuple(self._calls)

    def assert_consumed(self) -> None:
        remaining = {name: len(queue) for name, queue in self._scripts.items() if queue}
        if remaining:
            raise AssertionError(f"unconsumed fake outcomes: {remaining}")

    def _invoke(
        self, operation: str, arguments: tuple[object, ...], keywords: Mapping[str, object]
    ) -> Outcome[Any]:
        self._calls.append(DriverCall(operation, arguments, dict(keywords)))
        queue = self._scripts.get(operation)
        if not queue:
            raise AssertionError(f"no scripted outcome for {operation}")
        return queue.popleft()

    def attach(self, *, request_id: str, deadline_ms: int) -> Outcome[SessionInspection]:
        return cast(
            Outcome[SessionInspection],
            self._invoke("attach", (), {"request_id": request_id, "deadline_ms": deadline_ms}),
        )

    def inspect(self, *, request_id: str, deadline_ms: int) -> Outcome[SessionInspection]:
        return cast(
            Outcome[SessionInspection],
            self._invoke("inspect", (), {"request_id": request_id, "deadline_ms": deadline_ms}),
        )

    def detach(self, *, request_id: str, deadline_ms: int) -> Outcome[Detached]:
        return cast(
            Outcome[Detached],
            self._invoke("detach", (), {"request_id": request_id, "deadline_ms": deadline_ms}),
        )

    def inspect_project(
        self, project_id: str, *, request_id: str, deadline_ms: int
    ) -> Outcome[ProjectObservation]:
        return cast(
            Outcome[ProjectObservation],
            self._invoke(
                "inspect_project",
                (project_id,),
                {"request_id": request_id, "deadline_ms": deadline_ms},
            ),
        )

    def inspect_chat(
        self,
        project_id: str,
        chat_id: str,
        *,
        request_id: str,
        deadline_ms: int,
        root_frame_id: str | None = None,
    ) -> Outcome[ChatObservation]:
        return cast(
            Outcome[ChatObservation],
            self._invoke(
                "inspect_chat",
                (project_id, chat_id),
                {
                    "request_id": request_id,
                    "deadline_ms": deadline_ms,
                    "root_frame_id": root_frame_id,
                },
            ),
        )

    def inspect_context(
        self, project_id: str, *, request_id: str, deadline_ms: int
    ) -> Outcome[ContextObservation]:
        return cast(
            Outcome[ContextObservation],
            self._invoke(
                "inspect_context",
                (project_id,),
                {"request_id": request_id, "deadline_ms": deadline_ms},
            ),
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
        deadline_ms: int,
        root_frame_id: str | None = None,
    ) -> Outcome[TurnResult]:
        return cast(
            Outcome[TurnResult],
            self._invoke(
                "submit_turn_wait",
                (project_id, chat_id, root_mode, prompt, authored_prompt_sha256),
                {
                    "request_id": request_id,
                    "deadline_ms": deadline_ms,
                    "root_frame_id": root_frame_id,
                },
            ),
        )

    def wait_turn(
        self,
        project_id: str,
        chat_id: str,
        continuation: TurnContinuation,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> Outcome[TurnResult]:
        return cast(
            Outcome[TurnResult],
            self._invoke(
                "wait_turn",
                (project_id, chat_id, continuation),
                {"request_id": request_id, "deadline_ms": deadline_ms},
            ),
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
        deadline_ms: int,
    ) -> Outcome[ApprovalResolved]:
        return cast(
            Outcome[ApprovalResolved],
            self._invoke(
                "resolve_approval",
                (project_id, chat_id, root_frame_id, card_id, decision),
                {
                    "request_id": request_id,
                    "expected_fingerprint": expected_fingerprint,
                    "deadline_ms": deadline_ms,
                },
            ),
        )

    def create_project(
        self, name: str, *, request_id: str, deadline_ms: int
    ) -> Outcome[ProjectObservation]:
        return cast(
            Outcome[ProjectObservation],
            self._invoke(
                "create_project",
                (name,),
                {"request_id": request_id, "deadline_ms": deadline_ms},
            ),
        )

    def upload_attachment(
        self,
        project_id: str,
        chat_id: str,
        source_path: str | Path,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> Outcome[AttachmentAccepted]:
        return cast(
            Outcome[AttachmentAccepted],
            self._invoke(
                "upload_attachment",
                (project_id, chat_id, source_path),
                {"request_id": request_id, "deadline_ms": deadline_ms},
            ),
        )

    def update_enabled_skills(
        self,
        project_id: str,
        enabled_skills: frozenset[str],
        *,
        request_id: str,
        expected_before_hash: str,
        deadline_ms: int,
    ) -> Outcome[ContextUpdate]:
        return cast(
            Outcome[ContextUpdate],
            self._invoke(
                "update_enabled_skills",
                (project_id, tuple(enabled_skills)),
                {
                    "request_id": request_id,
                    "expected_before_hash": expected_before_hash,
                    "deadline_ms": deadline_ms,
                },
            ),
        )

    def new_chat(
        self, project_id: str, *, request_id: str, deadline_ms: int
    ) -> Outcome[ChatObservation]:
        return cast(
            Outcome[ChatObservation],
            self._invoke(
                "new_chat",
                (project_id,),
                {"request_id": request_id, "deadline_ms": deadline_ms},
            ),
        )

    def open_chat(
        self, project_id: str, chat_id: str, *, request_id: str, deadline_ms: int
    ) -> Outcome[ChatObservation]:
        return cast(
            Outcome[ChatObservation],
            self._invoke(
                "open_chat",
                (project_id, chat_id),
                {"request_id": request_id, "deadline_ms": deadline_ms},
            ),
        )
