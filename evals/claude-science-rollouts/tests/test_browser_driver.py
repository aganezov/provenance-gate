"""Offline tests for the ``TypedBrowserDriver`` boundary-to-runtime adapter.

Every case drives the adapter over a stub boundary session that returns canned
``browser.client`` outcome objects, so the tests assert only the mapping into runtime
``orchestration.models`` types — no browser subprocess is involved.
"""

from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace

from claude_science_rollouts.browser import client as boundary
from claude_science_rollouts.browser.protocol import BrowserError
from claude_science_rollouts.orchestration import models as runtime
from claude_science_rollouts.orchestration.browser_driver import TypedBrowserDriver
from claude_science_rollouts.orchestration.driver import BrowserDriver

_PROJECT = "project-1"
_CHAT = "chat-1"
_ROOT = "root-1"
_ORIGIN = "http://127.0.0.1:8875"


class StubSession:
    """Stand in for ``BrowserSession``: hand back scripted boundary outcomes, record every call."""

    def __init__(self, **outcomes: object) -> None:
        self.client = SimpleNamespace(session_id="session-1", origin=_ORIGIN)
        self.attached = False
        self._outcomes = outcomes
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def _dispatch(self, operation: str, *args: object, **kwargs: object) -> object:
        self.calls.append((operation, args, kwargs))
        value = self._outcomes[operation]
        if isinstance(value, list):
            value = value.pop(0)
        if operation == "attach" and value.outcome == "completed":
            self.attached = True
        if operation == "detach" and value.outcome == "completed" and value.detached:
            self.attached = False
        return value

    def __getattr__(self, operation: str):
        return lambda *args, **kwargs: self._dispatch(operation, *args, **kwargs)


def _operation(result: object) -> boundary.BrowserOperationOutcome:
    return boundary.BrowserOperationOutcome("completed", result, None, 7, 11)


def _observation(observation: object) -> boundary.BrowserObservationOutcome:
    return boundary.BrowserObservationOutcome("completed", observation, None, 7, 11)


def _boundary_error(retryable: bool) -> BrowserError:
    return BrowserError("BOUNDARY_FAILURE", "boundary failed", retryable, {"safe": True})


def _settled_turn(prompt: str) -> boundary.TurnResult:
    authored = sha256(prompt.encode()).hexdigest()
    return boundary.TurnResult(
        project_id=_PROJECT,
        chat_id=_CHAT,
        root_frame_id=_ROOT,
        turn_state="settled",
        root_created=True,
        delivery=boundary.DeliveryProof(_ROOT, authored, "delivery", "user-1"),
        settled=boundary.SettledProof(True, 3, "assistant-1"),
        approval=None,
        continuation=None,
    )


def _approval_turn(prompt: str) -> boundary.TurnResult:
    authored = sha256(prompt.encode()).hexdigest()
    delivery = boundary.DeliveryProof(_ROOT, authored, "delivery", "user-1")
    continuation = boundary.TurnContinuation(
        _PROJECT, _CHAT, _ROOT, authored, "delivery", "user-1", None
    )
    card = boundary.ApprovalCard("card-1", "f" * 64, "Run analysis", "tool")
    return boundary.TurnResult(
        project_id=_PROJECT,
        chat_id=_CHAT,
        root_frame_id=_ROOT,
        turn_state="approval_required",
        root_created=True,
        delivery=delivery,
        settled=None,
        approval=boundary.ApprovalObservation((card,)),
        continuation=continuation,
    )


def test_settled_turn_maps_result_and_timing() -> None:
    prompt = "construct the artifact"
    session = StubSession(submit_turn_wait=_operation(_settled_turn(prompt)))
    session.attached = True
    driver = TypedBrowserDriver(session)  # type: ignore[arg-type]

    outcome = driver.submit_turn_wait(
        _PROJECT,
        _CHAT,
        "new",
        prompt,
        sha256(prompt.encode()).hexdigest(),
        request_id="submit-1",
        deadline_ms=100,
    )

    assert outcome.completed
    assert isinstance(outcome.result, runtime.TurnResult)
    assert outcome.result.turn_state == "settled"
    assert outcome.result.settled is not None
    assert outcome.result.settled.new_response_control_id == "assistant-1"
    assert outcome.result.delivery is not None
    assert outcome.result.continuation is None
    # timing crosses the boundary intact, including the derived transport overhead.
    assert outcome.timing.boundary_elapsed_ms == 7
    assert outcome.timing.wall_elapsed_ms == 11
    assert outcome.timing.transport_overhead_ms == 4


def test_approval_required_turn_maps_cards_and_continuation() -> None:
    prompt = "run one approved turn"
    session = StubSession(submit_turn_wait=_operation(_approval_turn(prompt)))
    session.attached = True
    driver = TypedBrowserDriver(session)  # type: ignore[arg-type]

    outcome = driver.submit_turn_wait(
        _PROJECT,
        _CHAT,
        "new",
        prompt,
        sha256(prompt.encode()).hexdigest(),
        request_id="submit-1",
        deadline_ms=100,
    )

    assert outcome.completed
    assert isinstance(outcome.result, runtime.TurnResult)
    assert outcome.result.turn_state == "approval_required"
    assert outcome.result.settled is None
    assert outcome.result.approval is not None
    assert isinstance(outcome.result.approval, runtime.ApprovalObservation)
    (card,) = outcome.result.approval.cards
    assert isinstance(card, runtime.ApprovalCard)
    assert card.card_id == "card-1"
    assert card.fingerprint == "f" * 64
    # a delivered, unsettled turn keeps a continuation so the wait resumes without replay.
    assert isinstance(outcome.result.continuation, runtime.TurnContinuation)
    assert outcome.result.continuation.normalized_user_turn_id == "user-1"


def test_chat_observation_maps_transcript_into_runtime_types() -> None:
    observation = boundary.ChatObservation(
        project_id=_PROJECT,
        chat_id=_CHAT,
        transcript=(
            boundary.TurnObservation("turn-user", "user", "Question", False),
            boundary.TurnObservation("turn-assistant", "assistant", "Answer", False),
        ),
        user_turn_count=1,
        composer_empty=True,
        root_frame_id=_ROOT,
        response_control_id="turn-assistant",
        current_turn_state="settled",
        approval_cards=(),
    )
    session = StubSession(inspect_chat=_observation(observation))
    session.attached = True
    driver = TypedBrowserDriver(session)  # type: ignore[arg-type]

    outcome = driver.inspect_chat(
        _PROJECT, _CHAT, request_id="chat-1", deadline_ms=100, root_frame_id=_ROOT
    )

    assert outcome.completed
    assert isinstance(outcome.result, runtime.ChatObservation)
    assert outcome.result.response_control_id == "turn-assistant"
    assert all(isinstance(turn, runtime.TurnObservation) for turn in outcome.result.transcript)
    assert [turn.role for turn in outcome.result.transcript] == ["user", "assistant"]
    # the boundary receives the optional root identity as a keyword, not a positional argument.
    operation, args, kwargs = session.calls[0]
    assert operation == "inspect_chat"
    assert args == (_PROJECT, _CHAT)
    assert kwargs["root_frame_id"] == _ROOT


def test_select_model_maps_confirmed_runtime_selection() -> None:
    selected = boundary.ModelSelection(
        _PROJECT, _CHAT, "Research Fast", "Research Default", True, True
    )
    session = StubSession(select_model=_operation(selected))
    session.attached = True
    driver = TypedBrowserDriver(session)  # type: ignore[arg-type]

    outcome = driver.select_model(
        _PROJECT, _CHAT, "Research Fast", request_id="model-1", deadline_ms=100
    )

    assert outcome.completed
    assert isinstance(outcome.result, runtime.ModelSelection)
    assert outcome.result.model_label == "Research Fast"
    assert outcome.result.previous_model_label == "Research Default"
    assert outcome.result.changed is True
    assert outcome.result.confirmed is True
    assert session.calls[0] == (
        "select_model",
        (_PROJECT, _CHAT, "Research Fast"),
        {"request_id": "model-1", "deadline_ms": 100},
    )


def test_non_completed_outcomes_carry_errors_without_a_result() -> None:
    not_started = boundary.BrowserObservationOutcome(
        "not_started", None, _boundary_error(retryable=True), 2, 5
    )
    unknown = boundary.BrowserOperationOutcome(
        "unknown_outcome", None, _boundary_error(retryable=False), 4, 9
    )
    session = StubSession(inspect_project=not_started, upload_attachment=unknown)
    session.attached = True
    driver = TypedBrowserDriver(session)  # type: ignore[arg-type]

    inspected = driver.inspect_project(_PROJECT, request_id="inspect-1", deadline_ms=100)
    uploaded = driver.upload_attachment(
        _PROJECT, _CHAT, "/tmp/input.csv", request_id="upload-1", deadline_ms=100
    )

    assert inspected.outcome == "not_started"
    assert inspected.result is None
    assert inspected.error is not None and inspected.error.retryable
    assert inspected.error.evidence == {"safe": True}
    assert uploaded.outcome == "unknown_outcome"
    assert uploaded.result is None
    assert uploaded.error is not None and not uploaded.error.retryable


def test_attach_and_detach_map_session_lifecycle() -> None:
    attach = boundary.SessionInspectionOutcome(
        "completed", boundary.SessionInspection(True, _ORIGIN, True), None, 3, 7
    )
    detach = boundary.SessionDetachOutcome("completed", True, None, 2, 5)
    session = StubSession(attach=attach, detach=detach)
    driver = TypedBrowserDriver(session)  # type: ignore[arg-type]

    attached = driver.attach(request_id="attach-1", deadline_ms=100)
    detached = driver.detach(request_id="detach-1", deadline_ms=100)

    assert attached.completed and isinstance(attached.result, runtime.SessionInspection)
    assert attached.result.authenticated
    assert detached.completed and isinstance(detached.result, runtime.Detached)
    assert detached.result.detached is True
    assert [call[0] for call in session.calls] == ["attach", "detach"]


def test_wait_turn_lowers_runtime_continuation_to_the_boundary() -> None:
    prompt = "resume the turn"
    authored = sha256(prompt.encode()).hexdigest()
    continuation = runtime.TurnContinuation(
        _PROJECT, _CHAT, _ROOT, authored, "delivery", "user-1", None
    )
    session = StubSession(wait_turn=_operation(_settled_turn(prompt)))
    session.attached = True
    driver = TypedBrowserDriver(session)  # type: ignore[arg-type]

    outcome = driver.wait_turn(
        _PROJECT, _CHAT, continuation, request_id="wait-1", deadline_ms=100
    )

    assert outcome.completed
    assert [call[0] for call in session.calls] == ["wait_turn"]
    lowered = session.calls[0][1][2]
    assert isinstance(lowered, boundary.TurnContinuation)
    assert lowered.authored_prompt_sha256 == authored
    assert lowered.baseline_response_control_id is None


def test_deferred_and_unattached_operations_fail_closed_without_touching_the_boundary() -> None:
    session = StubSession()
    driver = TypedBrowserDriver(session)  # type: ignore[arg-type]

    update = driver.update_enabled_skills(
        _PROJECT,
        frozenset(),
        request_id="update-1",
        expected_before_hash="a" * 64,
        deadline_ms=100,
    )
    reopened = driver.open_chat(_PROJECT, _CHAT, request_id="open-1", deadline_ms=100)
    detached = driver.detach(request_id="detach-1", deadline_ms=100)

    assert update.outcome == reopened.outcome == detached.outcome == "not_started"
    assert update.error is not None and update.error.code == "OPERATION_NOT_IMPLEMENTED"
    assert session.calls == []


def test_typed_driver_satisfies_the_runtime_protocol() -> None:
    driver = TypedBrowserDriver(StubSession())  # type: ignore[arg-type]
    assert isinstance(driver, BrowserDriver)
    assert driver.session_id == "session-1"
    assert driver.origin == _ORIGIN
