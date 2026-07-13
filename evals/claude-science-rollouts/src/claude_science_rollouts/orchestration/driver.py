"""Structural protocol owned by the runtime control plane."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import (
    ApprovalDecision,
    ApprovalResolved,
    AttachmentAccepted,
    ChatObservation,
    ContextObservation,
    ContextUpdate,
    Detached,
    ModelSelection,
    Outcome,
    ProjectObservation,
    RootMode,
    SessionInspection,
    TurnContinuation,
    TurnResult,
)


@runtime_checkable
class BrowserDriver(Protocol):
    """One-session driver boundary; implementations own no orchestration policy."""

    session_id: str
    origin: str

    def attach(self, *, request_id: str, deadline_ms: int) -> Outcome[SessionInspection]: ...

    def inspect(self, *, request_id: str, deadline_ms: int) -> Outcome[SessionInspection]: ...

    def detach(self, *, request_id: str, deadline_ms: int) -> Outcome[Detached]: ...

    def inspect_project(
        self, project_id: str, *, request_id: str, deadline_ms: int
    ) -> Outcome[ProjectObservation]: ...

    def inspect_chat(
        self,
        project_id: str,
        chat_id: str,
        *,
        request_id: str,
        deadline_ms: int,
        root_frame_id: str | None = None,
    ) -> Outcome[ChatObservation]: ...

    def inspect_context(
        self, project_id: str, *, request_id: str, deadline_ms: int
    ) -> Outcome[ContextObservation]: ...

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
    ) -> Outcome[TurnResult]: ...

    def wait_turn(
        self,
        project_id: str,
        chat_id: str,
        continuation: TurnContinuation,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> Outcome[TurnResult]: ...

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
    ) -> Outcome[ApprovalResolved]: ...

    def create_project(
        self, name: str, *, request_id: str, deadline_ms: int
    ) -> Outcome[ProjectObservation]: ...

    def upload_attachment(
        self,
        project_id: str,
        chat_id: str,
        source_path: str | Path,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> Outcome[AttachmentAccepted]: ...

    def select_model(
        self,
        project_id: str,
        chat_id: str,
        model_label: str,
        *,
        request_id: str,
        deadline_ms: int,
    ) -> Outcome[ModelSelection]: ...

    def update_enabled_skills(
        self,
        project_id: str,
        enabled_skills: frozenset[str],
        *,
        request_id: str,
        expected_before_hash: str,
        deadline_ms: int,
    ) -> Outcome[ContextUpdate]: ...

    def new_chat(
        self, project_id: str, *, request_id: str, deadline_ms: int
    ) -> Outcome[ChatObservation]: ...

    def open_chat(
        self, project_id: str, chat_id: str, *, request_id: str, deadline_ms: int
    ) -> Outcome[ChatObservation]: ...
