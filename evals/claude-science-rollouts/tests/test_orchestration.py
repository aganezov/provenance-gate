"""Contract tests for the runtime driver seam, fake, and R1 state machine."""

from __future__ import annotations

import pytest

from claude_science_rollouts.orchestration.driver import BrowserDriver
from claude_science_rollouts.orchestration.fake import FakeBrowserDriver
from claude_science_rollouts.orchestration.models import (
    ApprovalCard,
    ApprovalObservation,
    ApprovalResolved,
    BrowserError,
    DeliveryProof,
    Outcome,
    SettledProof,
    Timing,
    TurnContinuation,
    TurnResult,
)
from claude_science_rollouts.orchestration.r1 import (
    R1ApprovalPolicy,
    R1LimitError,
    R1ProtocolError,
    R1TurnRequest,
    run_r1_turn,
)

_AUTHORED_SHA = "a" * 64
_OPAQUE_DELIVERY = "browser-owned:opaque:value"
_TIMING = Timing(boundary_elapsed_ms=10, wall_elapsed_ms=12, transport_overhead_ms=2)


def _completed(result):
    return Outcome(outcome="completed", result=result, error=None, timing=_TIMING)


def _unknown(code: str = "ambiguous_mutation"):
    return Outcome(
        outcome="unknown_outcome",
        result=None,
        error=BrowserError(code, "outcome is ambiguous", False, {}),
        timing=_TIMING,
    )


def _delivery(
    root: str = "root-1",
    *,
    authored_sha: str = _AUTHORED_SHA,
    delivery_sha: str = _OPAQUE_DELIVERY,
    turn_id: str = "turn-user-1",
) -> DeliveryProof:
    return DeliveryProof(root, authored_sha, delivery_sha, turn_id)


def _continuation(
    root: str = "root-1",
    turn_id: str = "turn-user-1",
    *,
    authored_sha: str = _AUTHORED_SHA,
    delivery_sha: str = _OPAQUE_DELIVERY,
) -> TurnContinuation:
    return TurnContinuation(
        project_id="project-1",
        chat_id="chat-1",
        root_frame_id=root,
        authored_prompt_sha256=authored_sha,
        delivery_text_sha256=delivery_sha,
        normalized_user_turn_id=turn_id,
        baseline_response_control_id="control-0",
    )


def _settled(
    root: str = "root-1",
    *,
    root_created: bool = True,
    authored_sha: str = _AUTHORED_SHA,
) -> TurnResult:
    return TurnResult(
        project_id="project-1",
        chat_id="chat-1",
        root_frame_id=root,
        turn_state="settled",
        root_created=root_created,
        delivery=_delivery(root, authored_sha=authored_sha),
        settled=SettledProof(True, 3, "control-1"),
        approval=None,
        continuation=None,
    )


def _approval(card_id: str, fingerprint: str, *, turn_id: str) -> TurnResult:
    card = ApprovalCard(card_id, fingerprint, "Run code", "tool")
    continuation = _continuation(turn_id=turn_id)
    return TurnResult(
        project_id="project-1",
        chat_id="chat-1",
        root_frame_id="root-1",
        turn_state="approval_required",
        root_created=True,
        delivery=DeliveryProof("root-1", _AUTHORED_SHA, _OPAQUE_DELIVERY, turn_id),
        settled=None,
        approval=ApprovalObservation((card,)),
        continuation=continuation,
    )


def _approval_with_cards(cards: tuple[ApprovalCard, ...]) -> TurnResult:
    return TurnResult(
        project_id="project-1",
        chat_id="chat-1",
        root_frame_id="root-1",
        turn_state="approval_required",
        root_created=True,
        delivery=_delivery(),
        settled=None,
        approval=ApprovalObservation(cards),
        continuation=_continuation(),
    )


def _unsettled(state: str, *, root: str = "root-1") -> TurnResult:
    return TurnResult(
        project_id="project-1",
        chat_id="chat-1",
        root_frame_id=root,
        turn_state=state,
        root_created=True,
        delivery=_delivery(root),
        settled=None,
        approval=None,
        continuation=_continuation(root),
    )


def _resolved(card_id: str, decision: str) -> ApprovalResolved:
    return ApprovalResolved(
        project_id="project-1",
        chat_id="chat-1",
        root_frame_id="root-1",
        card_id=card_id,
        decision=decision,
        verified_cleared=True,
    )


def _request(**overrides) -> R1TurnRequest:
    values = {
        "project_id": "project-1",
        "chat_id": "chat-1",
        "root_mode": "new",
        "prompt": "perform the turn",
        "authored_prompt_sha256": _AUTHORED_SHA,
        "request_id_prefix": "episode-1.turn-1",
        "deadline_ms": 30_000,
    }
    values.update(overrides)
    return R1TurnRequest(**values)


def test_fake_structurally_conforms_to_complete_driver_protocol() -> None:
    driver = FakeBrowserDriver("session-1", "http://127.0.0.1:8765", {})
    assert isinstance(driver, BrowserDriver)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: R1ApprovalPolicy("allow_for_conversation", 1.5),
        lambda: R1ApprovalPolicy("deny", 0.0),
        lambda: _request(deadline_ms=1.5),
        lambda: _request(max_waits=1.5),
    ],
)
def test_r1_policy_bounds_reject_fractional_values(factory) -> None:
    with pytest.raises(ValueError):
        factory()


def test_outcome_envelope_rejects_inconsistent_shapes() -> None:
    error = BrowserError("not_ready", "not ready", True, {})
    with pytest.raises(ValueError, match="requires only a result"):
        Outcome(outcome="completed", result=None, error=error, timing=_TIMING)
    with pytest.raises(ValueError, match="cannot be retryable"):
        Outcome(outcome="unknown_outcome", result=None, error=error, timing=_TIMING)


def test_delivery_hash_is_opaque_transport_data() -> None:
    continuation = _continuation()
    proof = _delivery()
    assert continuation.delivery_text_sha256 == _OPAQUE_DELIVERY
    assert proof.delivery_text_sha256 == _OPAQUE_DELIVERY


def test_turn_result_enforces_continuation_and_root_identity() -> None:
    with pytest.raises(ValueError, match="continuation is present exactly"):
        TurnResult(
            project_id="project-1",
            chat_id="chat-1",
            root_frame_id="root-1",
            turn_state="busy",
            root_created=True,
            delivery=_delivery(),
            settled=None,
            approval=None,
            continuation=None,
        )
    with pytest.raises(ValueError, match="root identity"):
        TurnResult(
            project_id="project-1",
            chat_id="chat-1",
            root_frame_id="root-1",
            turn_state="busy",
            root_created=True,
            delivery=_delivery("root-other"),
            settled=None,
            approval=None,
            continuation=_continuation(),
        )


@pytest.mark.parametrize(
    ("delivery", "continuation"),
    [
        (
            _delivery(authored_sha="b" * 64),
            _continuation(authored_sha="c" * 64),
        ),
        (
            _delivery(delivery_sha="opaque-a"),
            _continuation(delivery_sha="opaque-b"),
        ),
        (
            _delivery(turn_id="turn-user-1"),
            _continuation(turn_id="turn-user-2"),
        ),
    ],
)
def test_turn_result_rejects_mismatched_prompt_proof_identities(
    delivery: DeliveryProof,
    continuation: TurnContinuation,
) -> None:
    with pytest.raises(ValueError, match="prompt identities"):
        TurnResult(
            project_id="project-1",
            chat_id="chat-1",
            root_frame_id="root-1",
            turn_state="busy",
            root_created=True,
            delivery=delivery,
            settled=None,
            approval=None,
            continuation=continuation,
        )


def test_r1_rejects_result_for_different_authored_prompt() -> None:
    driver = FakeBrowserDriver(
        "session-1",
        "http://127.0.0.1:8765",
        {"submit_turn_wait": [_completed(_settled(authored_sha="b" * 64))]},
    )

    with pytest.raises(R1ProtocolError, match="authored prompt identity"):
        run_r1_turn(driver, _request())

    assert [call.operation for call in driver.calls] == ["submit_turn_wait"]


def test_r1_settled_submission_submits_exactly_once() -> None:
    final = _completed(_settled())
    driver = FakeBrowserDriver(
        "session-1",
        "http://127.0.0.1:8765",
        {"submit_turn_wait": [final]},
    )

    execution = run_r1_turn(driver, _request())

    assert execution.final is final
    assert execution.wait_count == 0
    assert [call.operation for call in driver.calls] == ["submit_turn_wait"]
    assert driver.calls[0].arguments[4] == _AUTHORED_SHA
    driver.assert_consumed()


def test_r1_existing_root_is_forwarded_and_must_not_be_recreated() -> None:
    final = _completed(_settled(root_created=False))
    driver = FakeBrowserDriver(
        "session-1",
        "http://127.0.0.1:8765",
        {"submit_turn_wait": [final]},
    )

    execution = run_r1_turn(
        driver,
        _request(root_mode="existing", root_frame_id="root-1"),
    )

    assert execution.final is final
    assert driver.calls[0].keywords["root_frame_id"] == "root-1"
    driver.assert_consumed()


def test_r1_approval_uses_exact_card_then_waits_without_replay() -> None:
    first = _approval("card-1", "fingerprint-1", turn_id="turn-user-1")
    continuation = first.continuation
    driver = FakeBrowserDriver(
        "session-1",
        "http://127.0.0.1:8765",
        {
            "submit_turn_wait": [_completed(first)],
            "resolve_approval": [_completed(_resolved("card-1", "allow_for_conversation"))],
            "wait_turn": [_completed(_settled())],
        },
    )

    execution = run_r1_turn(
        driver,
        _request(),
        approval_policy=R1ApprovalPolicy("allow_for_conversation", 1),
    )

    assert execution.final.result.turn_state == "settled"
    assert [call.operation for call in driver.calls] == [
        "submit_turn_wait",
        "resolve_approval",
        "wait_turn",
    ]
    approval_call = driver.calls[1]
    assert approval_call.arguments[3:] == ("card-1", "allow_for_conversation")
    assert approval_call.keywords["expected_fingerprint"] == "fingerprint-1"
    assert driver.calls[2].arguments[2] is continuation
    assert continuation.delivery_text_sha256 == _OPAQUE_DELIVERY
    driver.assert_consumed()


def test_r1_approval_limit_denies_then_stops_policy_exceeded() -> None:
    first = _approval("card-1", "fingerprint-1", turn_id="turn-user-1")
    second = _approval("card-2", "fingerprint-2", turn_id="turn-user-2")
    driver = FakeBrowserDriver(
        "session-1",
        "http://127.0.0.1:8765",
        {
            "submit_turn_wait": [_completed(first)],
            "resolve_approval": [
                _completed(_resolved("card-1", "allow_for_conversation")),
                _completed(_resolved("card-2", "deny")),
            ],
            "wait_turn": [_completed(second)],
        },
    )

    execution = run_r1_turn(
        driver,
        _request(),
        approval_policy=R1ApprovalPolicy("allow_for_conversation", 1),
    )

    decisions = [call.arguments[4] for call in driver.calls if call.operation == "resolve_approval"]
    assert decisions == ["allow_for_conversation", "deny"]
    assert execution.stop_reason == "policy_exceeded"
    assert execution.final.result is second
    assert execution.wait_count == 1
    assert len(execution.approval_resolutions) == 2
    assert [call.operation for call in driver.calls].count("submit_turn_wait") == 1
    assert [call.operation for call in driver.calls].count("wait_turn") == 1
    driver.assert_consumed()


@pytest.mark.parametrize(
    "state",
    ["input_required", "indeterminate", "navigation_drift", "failed"],
)
def test_r1_returns_terminal_observations_without_polling(state: str) -> None:
    terminal = _completed(_unsettled(state))
    driver = FakeBrowserDriver(
        "session-1",
        "http://127.0.0.1:8765",
        {"submit_turn_wait": [terminal]},
    )

    execution = run_r1_turn(driver, _request())

    assert execution.final is terminal
    assert execution.stop_reason == "terminal_observation"
    assert execution.wait_count == 0
    assert [call.operation for call in driver.calls] == ["submit_turn_wait"]
    driver.assert_consumed()


@pytest.mark.parametrize(
    "cards",
    [
        (),
        (
            ApprovalCard("card-1", "fingerprint-1", "Run one", "tool"),
            ApprovalCard("card-2", "fingerprint-2", "Run two", "tool"),
        ),
        (
            ApprovalCard("card-1", "fingerprint-1", "Run one", "tool"),
            ApprovalCard("card-1", "fingerprint-1", "Run one", "tool"),
        ),
    ],
)
def test_r1_rejects_ambiguous_approval_cards(cards: tuple[ApprovalCard, ...]) -> None:
    driver = FakeBrowserDriver(
        "session-1",
        "http://127.0.0.1:8765",
        {"submit_turn_wait": [_completed(_approval_with_cards(cards))]},
    )

    with pytest.raises(R1ProtocolError, match="exactly one actionable card"):
        run_r1_turn(driver, _request())

    assert [call.operation for call in driver.calls] == ["submit_turn_wait"]
    driver.assert_consumed()


def test_r1_wait_limit_stops_without_extra_wait_or_replay() -> None:
    busy = _completed(_unsettled("busy"))
    driver = FakeBrowserDriver(
        "session-1",
        "http://127.0.0.1:8765",
        {
            "submit_turn_wait": [busy],
            "wait_turn": [busy],
        },
    )

    with pytest.raises(R1LimitError, match="within 1 waits"):
        run_r1_turn(driver, _request(max_waits=1))

    assert [call.operation for call in driver.calls] == ["submit_turn_wait", "wait_turn"]
    driver.assert_consumed()


def test_r1_rejects_root_drift_after_first_observation() -> None:
    first = _approval("card-1", "fingerprint-1", turn_id="turn-user-1")
    driver = FakeBrowserDriver(
        "session-1",
        "http://127.0.0.1:8765",
        {
            "submit_turn_wait": [_completed(first)],
            "resolve_approval": [_completed(_resolved("card-1", "allow_for_conversation"))],
            "wait_turn": [_completed(_settled("root-other"))],
        },
    )

    with pytest.raises(R1ProtocolError, match="root identity mismatch"):
        run_r1_turn(
            driver,
            _request(),
            approval_policy=R1ApprovalPolicy("allow_for_conversation", 1),
        )

    assert [call.operation for call in driver.calls] == [
        "submit_turn_wait",
        "resolve_approval",
        "wait_turn",
    ]
    driver.assert_consumed()


def test_r1_propagates_unknown_submission_without_retry() -> None:
    unknown = _unknown()
    driver = FakeBrowserDriver(
        "session-1",
        "http://127.0.0.1:8765",
        {"submit_turn_wait": [unknown]},
    )

    execution = run_r1_turn(driver, _request())

    assert execution.final is unknown
    assert execution.final.outcome == "unknown_outcome"
    assert len(driver.calls) == 1
    driver.assert_consumed()


def test_r1_propagates_unknown_approval_without_wait_or_replay() -> None:
    first = _approval("card-1", "fingerprint-1", turn_id="turn-user-1")
    driver = FakeBrowserDriver(
        "session-1",
        "http://127.0.0.1:8765",
        {
            "submit_turn_wait": [_completed(first)],
            "resolve_approval": [_unknown("approval_ambiguous")],
        },
    )

    execution = run_r1_turn(driver, _request())

    assert execution.final.outcome == "unknown_outcome"
    assert [call.operation for call in driver.calls] == [
        "submit_turn_wait",
        "resolve_approval",
    ]
    driver.assert_consumed()


def test_r1_rejects_mismatched_completed_approval_echo() -> None:
    first = _approval("card-1", "fingerprint-1", turn_id="turn-user-1")
    driver = FakeBrowserDriver(
        "session-1",
        "http://127.0.0.1:8765",
        {
            "submit_turn_wait": [_completed(first)],
            "resolve_approval": [_completed(_resolved("wrong-card", "deny"))],
        },
    )

    with pytest.raises(R1ProtocolError, match="approval result identity"):
        run_r1_turn(driver, _request())

    assert [call.operation for call in driver.calls] == [
        "submit_turn_wait",
        "resolve_approval",
    ]
