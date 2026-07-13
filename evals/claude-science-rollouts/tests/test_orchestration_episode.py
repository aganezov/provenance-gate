"""Fake-driven episode execution, finalization, and durable-evidence tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from claude_science_rollouts.orchestration.episode import (
    EpisodeConfig,
    EpisodeExecutor,
    approval_policy_for_scenario,
    matches_response_rule,
)
from claude_science_rollouts.orchestration.fake import DriverCall, FakeBrowserDriver
from claude_science_rollouts.orchestration.models import (
    ApprovalCard,
    ApprovalObservation,
    ApprovalResolved,
    AttachmentAccepted,
    BrowserError,
    ChatObservation,
    DeliveryProof,
    Detached,
    Outcome,
    SessionInspection,
    SettledProof,
    Timing,
    TurnContinuation,
    TurnObservation,
    TurnResult,
)
from claude_science_rollouts.orchestration.prose import (
    ProseInterpretationRequest,
    ProseInterpretationResult,
    ProseInterpreter,
)
from claude_science_rollouts.persistence.responses import (
    PersistedInputRequest,
    PersistedResponse,
)
from claude_science_rollouts.persistence.snapshots import SnapshotBarrierConfig
from claude_science_rollouts.scenario.compiler import compile_scenario
from claude_science_rollouts.scenario.spec import (
    ApprovalPolicy,
    ResponseRule,
    Scenario,
    Session,
    Trial,
    Turn,
    load_scenario,
)
from operon_fixture import Operon, pbmc

_SCENARIO_PATH = Path(__file__).resolve().parents[1] / "scenarios" / "pbmc_figure_package.json"
_ORIGIN = "http://127.0.0.1:8765"
_PROJECT = "project-1"
_TIMING = Timing(10, 12, 2)


def _completed(result):
    return Outcome("completed", result, None, _TIMING)


def _unknown(code: str):
    return Outcome(
        "unknown_outcome",
        None,
        BrowserError(code, "ambiguous outcome", False, {}),
        _TIMING,
    )


def _seed_db(path: Path, *, complete_pbmc: bool = False, artifact: bool = False) -> None:
    operon = Operon(_PROJECT)
    if complete_pbmc:
        pbmc(operon)
    elif artifact:
        item = operon.artifact("a.csv")
        operon.version(item, 1, latest=True)
    operon.conn.commit()
    target = sqlite3.connect(path)
    try:
        operon.conn.backup(target)
    finally:
        target.close()
        operon.conn.close()


def _chat(chat_id: str) -> ChatObservation:
    return ChatObservation(
        project_id=_PROJECT,
        chat_id=chat_id,
        transcript=(),
        user_turn_count=0,
        composer_empty=True,
        root_frame_id=None,
        response_control_id=None,
        current_turn_state="settled",
        approval_cards=(),
    )


def _settled_turn(
    chat_id: str,
    root_id: str,
    prompt: str,
    sequence: int,
    *,
    root_created: bool,
) -> TurnResult:
    authored = hashlib.sha256(prompt.encode()).hexdigest()
    delivery = f"opaque-delivery-{sequence}"
    user_turn = f"user-turn-{sequence}"
    return TurnResult(
        project_id=_PROJECT,
        chat_id=chat_id,
        root_frame_id=root_id,
        turn_state="settled",
        root_created=root_created,
        delivery=DeliveryProof(root_id, authored, delivery, user_turn),
        settled=SettledProof(True, 3, f"control-{sequence}"),
        approval=None,
        continuation=None,
    )


def _unsettled_turn(
    chat_id: str,
    root_id: str,
    prompt: str,
    state: str,
    *,
    approval_cards: tuple[ApprovalCard, ...] = (),
    root_created: bool = True,
) -> TurnResult:
    authored = hashlib.sha256(prompt.encode()).hexdigest()
    delivery = "opaque-unsettled"
    user_turn = "user-unsettled"
    continuation = TurnContinuation(
        _PROJECT,
        chat_id,
        root_id,
        authored,
        delivery,
        user_turn,
        None,
    )
    return TurnResult(
        project_id=_PROJECT,
        chat_id=chat_id,
        root_frame_id=root_id,
        turn_state=state,
        root_created=root_created,
        delivery=DeliveryProof(root_id, authored, delivery, user_turn),
        settled=None,
        approval=(ApprovalObservation(approval_cards) if state == "approval_required" else None),
        continuation=continuation,
    )


def _minimal_scenario(*, approval_policy: ApprovalPolicy | None = None) -> Scenario:
    return Scenario(
        schema_version=1,
        scenario_id="minimal",
        tier="scientific",
        sessions=(Session("main", "new"),),
        construction=(Turn("main", "construction", "construct artifact"),),
        trial=Trial("main", "trial", {"bare": "run trial"}),
        checkpoints=(
            {
                "id": "gate",
                "mode": "gate",
                "after_turn_id": "construction",
                "assertions": [
                    {"kind": "version_exists", "artifact": "a.csv", "version": 1}
                ],
            },
        ),
        approval_policy=approval_policy or ApprovalPolicy("deny", 0),
    )


def _config(
    tmp_path: Path,
    source_db: Path,
    fixture: Path | None = None,
    *,
    halt_on_checkpoint_gate: bool = False,
) -> EpisodeConfig:
    return EpisodeConfig(
        episode_id="episode-1",
        project_id=_PROJECT,
        source_db=source_db,
        run_dir=tmp_path / "run",
        fixture_path=fixture,
        deadline_ms=30_000,
        snapshot=SnapshotBarrierConfig(poll_interval_seconds=0, timeout_seconds=2),
        halt_on_checkpoint_gate=halt_on_checkpoint_gate,
    )


def _base_scripts(chat_id: str = "chat-1") -> dict[str, list[Outcome]]:
    return {
        "attach": [_completed(SessionInspection(True, _ORIGIN, True))],
        "new_chat": [_completed(_chat(chat_id))],
        "detach": [_completed(Detached(True))],
    }


class _FakeResponseReader:
    """Scripted persisted-evidence reader: returns responses/input-requests keyed by user turn id.

    This stands in for the operon-backed reader so the episode's capture *wiring* is exercised — the
    reader is called at every settle point and its records land in the manifest — without needing a
    full transcript. ``snapshot_reads`` records which reduced snapshot the final-turn regrade read.
    """

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self.responses = responses or {}
        self.snapshot_reads: list[Path] = []

    def _response(self, **kwargs) -> PersistedResponse:
        user_turn_id = kwargs["user_turn_id"]
        text = self.responses.get(user_turn_id, "The requested work is complete.")
        return PersistedResponse(
            kwargs["project_id"],
            kwargs["root_frame_id"],
            user_turn_id,
            kwargs["authored_prompt_sha256"],
            f"assistant-{user_turn_id}",
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            2,
            None,
        )

    def _input_request(self, **kwargs) -> PersistedInputRequest:
        payload = {"question": "The outputs use different data versions. Continue?"}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return PersistedInputRequest(
            kwargs["project_id"],
            kwargs["root_frame_id"],
            kwargs["user_turn_id"],
            kwargs["authored_prompt_sha256"],
            f"assistant-{kwargs['user_turn_id']}",
            "I found a version conflict.",
            "input-1",
            "request_input",
            payload,
            hashlib.sha256(canonical.encode()).hexdigest(),
            2,
            None,
            "awaiting_user_response",
        )

    async def read(self, **kwargs) -> PersistedResponse:
        return self._response(**kwargs)

    async def read_input_request(self, **kwargs) -> PersistedInputRequest:
        return self._input_request(**kwargs)

    def read_from_snapshot(self, **kwargs) -> PersistedResponse:
        self.snapshot_reads.append(kwargs["snapshot_db"])
        return self._response(**kwargs)

    def read_input_request_from_snapshot(self, **kwargs) -> PersistedInputRequest:
        self.snapshot_reads.append(kwargs["snapshot_db"])
        return self._input_request(**kwargs)


class _FixedInterpreter:
    """Return one canned classifier reading regardless of the prose handed in."""

    def __init__(self, result: ProseInterpretationResult) -> None:
        self.result = result

    def interpret(self, _request: ProseInterpretationRequest) -> ProseInterpretationResult:
        return self.result


def _executor(
    driver: FakeBrowserDriver,
    responses: dict[str, str] | None = None,
    interpreter: ProseInterpreter | None = None,
) -> EpisodeExecutor:
    return EpisodeExecutor(driver, _FakeResponseReader(responses), interpreter)


def test_pbmc_episode_executes_full_plan_and_persists_compact_evidence(tmp_path: Path) -> None:
    scenario = load_scenario(_SCENARIO_PATH)
    source_db = tmp_path / "operon.db"
    fixture_bytes = b"deterministic external fixture bytes\n"
    assert scenario.fixture is not None
    fixture_spec = dict(scenario.fixture)
    fixture_spec["sha256"] = hashlib.sha256(fixture_bytes).hexdigest()
    scenario = replace(scenario, fixture=fixture_spec)
    fixture = tmp_path / scenario.fixture["filename"]
    fixture.write_bytes(fixture_bytes)
    _seed_db(source_db, complete_pbmc=True)
    plan = compile_scenario(scenario)
    chat_ids = {session.id: f"chat-{index}" for index, session in enumerate(scenario.sessions, 1)}
    root_ids = {session.id: f"root-{index}" for index, session in enumerate(scenario.sessions, 1)}
    seen_sessions: set[str] = set()
    submit_outcomes = []
    expected_prompts = []
    sequence = 0
    for step in plan:
        if step.op != "submit":
            continue
        assert step.session and step.turn_id and step.prompt is not None
        sequence += 1
        created = step.session not in seen_sessions
        seen_sessions.add(step.session)
        submit_outcomes.append(
            _completed(
                _settled_turn(
                    chat_ids[step.session],
                    root_ids[step.session],
                    step.prompt,
                    sequence,
                    root_created=created,
                )
            )
        )
        expected_prompts.append(step.prompt)
        if step.turn_id == "strict-ifn-branch":
            rule = scenario.response_rules[0]
            sequence += 1
            submit_outcomes.append(
                _completed(
                    _settled_turn(
                        chat_ids[step.session],
                        root_ids[step.session],
                        rule.reply,
                        sequence,
                        root_created=False,
                    )
                )
            )
            expected_prompts.append(rule.reply)

    offer = TurnObservation(
        "assistant-offer",
        "assistant",
        "I can regenerate the sibling panels if you want.",
        False,
    )
    inspection = ChatObservation(
        _PROJECT,
        chat_ids["scientific-update"],
        (offer,),
        3,
        True,
        root_ids["scientific-update"],
        "control-offer",
        "settled",
        (),
    )
    scripts = {
        "attach": [_completed(SessionInspection(True, _ORIGIN, True))],
        "new_chat": [_completed(_chat(chat_ids[s.id])) for s in scenario.sessions],
        "upload_attachment": [
            _completed(
                AttachmentAccepted(
                    _PROJECT,
                    chat_ids["initial-build"],
                    fixture.name,
                    True,
                )
            )
        ],
        "submit_turn_wait": submit_outcomes,
        "inspect_chat": [_completed(inspection)],
        "detach": [_completed(Detached(True))],
    }
    driver = FakeBrowserDriver("session-1", _ORIGIN, scripts)
    reader = _FakeResponseReader()

    result = asyncio.run(
        EpisodeExecutor(driver, reader).run(scenario, _config(tmp_path, source_db, fixture))
    )

    assert result.terminal_reason == "completed"
    assert result.detach_outcome == "completed"
    calls = driver.calls
    assert [call.operation for call in calls].count("new_chat") == 4
    assert [call.operation for call in calls].count("upload_attachment") == 1
    assert [call.operation for call in calls].count("submit_turn_wait") == 11
    submitted_prompts = [
        call.arguments[3] for call in calls if call.operation == "submit_turn_wait"
    ]
    assert submitted_prompts == expected_prompts
    upload_index = next(i for i, call in enumerate(calls) if call.operation == "upload_attachment")
    first_submit = next(i for i, call in enumerate(calls) if call.operation == "submit_turn_wait")
    assert upload_index < first_submit
    assert calls[-1].operation == "detach"
    driver.assert_consumed()

    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["terminal_reason"] == "completed"
    assert len(manifest["turns"]) == 11
    assert [item["id"] for item in manifest["checkpoints"]] == [
        "baseline-qc",
        "baseline-branches",
        "strict-qc-reversion",
        "strict-ifn-panel",
    ]
    assert all(item["passed"] for item in manifest["checkpoints"])
    assert all(item["stability_attempts"] == 2 for item in manifest["checkpoints"])
    assert manifest["final_snapshot"]["stability_attempts"] == 2

    # every settled turn now carries the persisted-response evidence read at its settle point.
    assert all(item["persisted_response"] is not None for item in manifest["turns"])
    assert all(item["persisted_input_request"] is None for item in manifest["turns"])
    first_response = manifest["turns"][0]["persisted_response"]
    assert len(first_response["assistant_text_sha256"]) == 64
    assert first_response["stability_attempts"] == 2
    assert first_response["assistant_message_id"] == "assistant-user-turn-1"
    # the final turn's evidence is re-bound to the immutable reduced snapshot before finalizing.
    assert reader.snapshot_reads == [tmp_path / "run" / "snapshots" / "final" / "project.db"]

    # one settled snapshot persisted per checkpoint (for the post-hoc grader) plus the final one.
    checkpoint_ids = ["baseline-qc", "baseline-branches", "strict-qc-reversion", "strict-ifn-panel"]
    assert len(list((tmp_path / "run").rglob("*.db"))) == len(checkpoint_ids) + 1
    assert manifest["final_snapshot"]["path"] == "snapshots/final/project.db"
    for item in manifest["checkpoints"]:
        expected = tmp_path / "run" / "snapshots" / "checkpoints" / item["id"] / "project.db"
        assert item["snapshot_path"] == str(expected) and expected.is_file()
        assert len(item["snapshot_sha256"]) == 64
    assert not (tmp_path / "run" / ".snapshot-work").exists()


def test_pbmc_approval_policy_maps_to_python_owned_limit() -> None:
    policy = approval_policy_for_scenario(load_scenario(_SCENARIO_PATH))
    assert policy.action == "allow_for_conversation"
    assert policy.max_approvals == 8


@pytest.mark.parametrize(
    ("filename", "contents", "reason"),
    [
        ("wrong-name.csv", b"bytes", "fixture_filename_mismatch"),
        ("pbmc_tiny_seed.csv", b"wrong bytes", "fixture_sha256_mismatch"),
    ],
)
def test_pbmc_fixture_name_and_hash_fail_before_upload(
    tmp_path: Path,
    filename: str,
    contents: bytes,
    reason: str,
) -> None:
    scenario = load_scenario(_SCENARIO_PATH)
    source_db = tmp_path / "operon.db"
    _seed_db(source_db)
    fixture = tmp_path / filename
    fixture.write_bytes(contents)
    driver = FakeBrowserDriver("session-1", _ORIGIN, _base_scripts())

    result = asyncio.run(
        _executor(driver).run(scenario, _config(tmp_path, source_db, fixture))
    )

    assert result.terminal_reason == reason
    operations = [call.operation for call in driver.calls]
    assert operations == ["attach", "new_chat", "detach"]
    assert "upload_attachment" not in operations
    assert "submit_turn_wait" not in operations
    driver.assert_consumed()


def test_historical_response_offer_does_not_trigger_rule() -> None:
    rule = load_scenario(_SCENARIO_PATH).response_rules[0]
    observation = ChatObservation(
        _PROJECT,
        "chat-1",
        (
            TurnObservation(
                "assistant-old",
                "assistant",
                "I can regenerate the sibling panels.",
                False,
            ),
            TurnObservation(
                "assistant-latest",
                "assistant",
                "The requested IFN update is complete.",
                False,
            ),
        ),
        2,
        True,
        "root-1",
        "control-2",
        "settled",
        (),
    )
    assert matches_response_rule(rule, observation) is False


def test_historical_offer_with_newest_truncated_assistant_turn_does_not_match() -> None:
    rule = load_scenario(_SCENARIO_PATH).response_rules[0]
    observation = ChatObservation(
        _PROJECT,
        "chat-1",
        (
            TurnObservation(
                "assistant-old",
                "assistant",
                "I can regenerate the sibling panels.",
                False,
            ),
            TurnObservation(
                "assistant-truncated",
                "assistant",
                "I can regenerate",
                True,
            ),
        ),
        2,
        True,
        "root-1",
        "control-2",
        "settled",
        (),
    )
    assert matches_response_rule(rule, observation) is False


@pytest.mark.parametrize("failure_point", ["submit", "wait", "approval"])
def test_ambiguous_turn_outcomes_stop_episode_and_detach(
    tmp_path: Path, failure_point: str
) -> None:
    scenario = _minimal_scenario()
    source_db = tmp_path / "operon.db"
    _seed_db(source_db, artifact=True)
    scripts = _base_scripts()
    prompt = scenario.construction[0].prompt
    if failure_point == "submit":
        scripts["submit_turn_wait"] = [_unknown("submit_ambiguous")]
    elif failure_point == "wait":
        scripts["submit_turn_wait"] = [
            _completed(_unsettled_turn("chat-1", "root-1", prompt, "busy"))
        ]
        scripts["wait_turn"] = [_unknown("wait_ambiguous")]
    else:
        card = ApprovalCard("card-1", "fingerprint-1", "Run", "tool")
        scripts["submit_turn_wait"] = [
            _completed(
                _unsettled_turn(
                    "chat-1",
                    "root-1",
                    prompt,
                    "approval_required",
                    approval_cards=(card,),
                )
            )
        ]
        scripts["resolve_approval"] = [_unknown("approval_ambiguous")]
    driver = FakeBrowserDriver("session-1", _ORIGIN, scripts)

    result = asyncio.run(_executor(driver).run(scenario, _config(tmp_path, source_db)))

    assert result.terminal_reason == "turn_unknown_outcome"
    operations = [call.operation for call in driver.calls]
    assert operations[-1] == "detach"
    assert operations.count("detach") == 1
    assert operations.count("submit_turn_wait") == 1
    assert json.loads(result.manifest_path.read_text())["terminal_reason"] == "turn_unknown_outcome"
    driver.assert_consumed()


def test_stale_new_chat_stops_before_submission(tmp_path: Path) -> None:
    scenario = _minimal_scenario()
    source_db = tmp_path / "operon.db"
    _seed_db(source_db, artifact=True)
    stale = ChatObservation(
        _PROJECT,
        "chat-stale",
        (),
        1,
        False,
        "root-stale",
        "control-stale",
        "settled",
        (),
    )
    scripts = {
        "attach": [_completed(SessionInspection(True, _ORIGIN, True))],
        "new_chat": [_completed(stale)],
        "detach": [_completed(Detached(True))],
    }
    driver = FakeBrowserDriver("session-1", _ORIGIN, scripts)

    result = asyncio.run(_executor(driver).run(scenario, _config(tmp_path, source_db)))

    assert result.terminal_reason == "new_chat_not_fresh"
    assert [call.operation for call in driver.calls] == ["attach", "new_chat", "detach"]
    driver.assert_consumed()


def test_completed_detach_false_is_a_terminal_failure(tmp_path: Path) -> None:
    scenario = _minimal_scenario()
    source_db = tmp_path / "operon.db"
    _seed_db(source_db, artifact=True)
    scripts = _base_scripts()
    scripts["submit_turn_wait"] = [_unknown("submit_ambiguous")]
    scripts["detach"] = [_completed(Detached(False))]
    driver = FakeBrowserDriver("session-1", _ORIGIN, scripts)

    result = asyncio.run(_executor(driver).run(scenario, _config(tmp_path, source_db)))

    assert result.terminal_reason == "detach_failed"
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["terminal_reason"] == "detach_failed"
    assert manifest["steps"][-1]["detached"] is False
    assert [call.operation for call in driver.calls].count("detach") == 1
    driver.assert_consumed()


@pytest.mark.parametrize(
    "state",
    ["input_required", "indeterminate", "navigation_drift", "failed"],
)
def test_terminal_turn_state_finalizes_and_detaches_without_later_steps(
    tmp_path: Path, state: str
) -> None:
    scenario = _minimal_scenario()
    source_db = tmp_path / "operon.db"
    _seed_db(source_db, artifact=True)
    scripts = _base_scripts()
    scripts["submit_turn_wait"] = [
        _completed(
            _unsettled_turn(
                "chat-1",
                "root-1",
                scenario.construction[0].prompt,
                state,
            )
        )
    ]
    driver = FakeBrowserDriver("session-1", _ORIGIN, scripts)

    result = asyncio.run(_executor(driver).run(scenario, _config(tmp_path, source_db)))

    assert result.terminal_reason == "terminal_observation"
    assert [call.operation for call in driver.calls] == [
        "attach",
        "new_chat",
        "submit_turn_wait",
        "detach",
    ]
    assert result.manifest_path.exists()
    driver.assert_consumed()


def test_checkpoint_failure_halts_when_gate_enabled(tmp_path: Path) -> None:
    # halt_on_checkpoint_gate is the opt-in early-stop capability: a failed GATE-mode checkpoint
    # stops the rollout before the trial. Generation leaves it off (next test).
    scenario = _minimal_scenario()
    source_db = tmp_path / "operon.db"
    _seed_db(source_db)
    scripts = _base_scripts()
    scripts["submit_turn_wait"] = [
        _completed(
            _settled_turn(
                "chat-1",
                "root-1",
                scenario.construction[0].prompt,
                1,
                root_created=True,
            )
        )
    ]
    driver = FakeBrowserDriver("session-1", _ORIGIN, scripts)

    result = asyncio.run(
        _executor(driver).run(scenario, _config(tmp_path, source_db, halt_on_checkpoint_gate=True))
    )

    assert result.terminal_reason == "checkpoint_failed_gate"
    assert [call.operation for call in driver.calls].count("submit_turn_wait") == 1
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["checkpoints"][0]["passed"] is False
    assert manifest["checkpoints"][0]["stability_attempts"] == 2
    assert [call.operation for call in driver.calls][-1] == "detach"
    driver.assert_consumed()


def test_failed_checkpoint_records_but_does_not_halt_by_default(tmp_path: Path) -> None:
    # the generation default: a failed gate checkpoint is recorded as evidence (with its own settled
    # snapshot) and the rollout runs to completion, so a post-hoc grader sees the whole divergent
    # construction — not a run truncated at the first stumble.
    scenario = _minimal_scenario()
    source_db = tmp_path / "operon.db"
    _seed_db(source_db)  # no artifact -> the gate's version_exists assertion fails
    scripts = _base_scripts()
    scripts["submit_turn_wait"] = [
        _completed(
            _settled_turn(
                "chat-1", "root-1", scenario.construction[0].prompt, 1, root_created=True
            )
        ),
        _completed(
            _settled_turn(
                "chat-1", "root-1", scenario.trial.variants["bare"], 3, root_created=False
            )
        ),
    ]
    driver = FakeBrowserDriver("session-1", _ORIGIN, scripts)

    result = asyncio.run(_executor(driver).run(scenario, _config(tmp_path, source_db)))

    assert result.terminal_reason == "completed"                     # not halted
    assert [call.operation for call in driver.calls].count("submit_turn_wait") == 2
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["checkpoints"][0]["passed"] is False   # ...but the failure is recorded
    # and the failed checkpoint still persisted its settled snapshot for the grader
    gate_snapshot = tmp_path / "run" / "snapshots" / "checkpoints" / "gate" / "project.db"
    assert gate_snapshot.is_file()
    assert len(manifest["checkpoints"][0]["snapshot_sha256"]) == 64
    driver.assert_consumed()


def test_busy_continuation_settles_without_prompt_replay(tmp_path: Path) -> None:
    scenario = _minimal_scenario()
    source_db = tmp_path / "operon.db"
    _seed_db(source_db, artifact=True)
    construction = scenario.construction[0].prompt
    scripts = _base_scripts()
    scripts["submit_turn_wait"] = [
        _completed(_unsettled_turn("chat-1", "root-1", construction, "busy")),
        _completed(
            _settled_turn(
                "chat-1", "root-1", scenario.trial.variants["bare"], 3, root_created=False
            )
        ),
    ]
    scripts["wait_turn"] = [
        _completed(_settled_turn("chat-1", "root-1", construction, 2, root_created=True))
    ]
    driver = FakeBrowserDriver("session-1", _ORIGIN, scripts)

    result = asyncio.run(_executor(driver).run(scenario, _config(tmp_path, source_db)))

    operations = [call.operation for call in driver.calls]
    assert result.terminal_reason == "completed"
    assert operations.count("submit_turn_wait") == 2
    assert operations.count("wait_turn") == 1
    prompts = [call.arguments[3] for call in driver.calls if call.operation == "submit_turn_wait"]
    assert prompts == [construction, scenario.trial.variants["bare"]]
    driver.assert_consumed()


def test_scenario_approval_budget_denies_excess_and_stops_episode(tmp_path: Path) -> None:
    policy = ApprovalPolicy("allow_for_conversation", 1)
    scenario = _minimal_scenario(approval_policy=policy)
    source_db = tmp_path / "operon.db"
    _seed_db(source_db, artifact=True)
    prompt = scenario.construction[0].prompt
    first = ApprovalCard("card-1", "fingerprint-1", "Run one", "tool")
    second = ApprovalCard("card-2", "fingerprint-2", "Run two", "tool")
    scripts = _base_scripts()
    scripts["submit_turn_wait"] = [
        _completed(
            _unsettled_turn(
                "chat-1", "root-1", prompt, "approval_required", approval_cards=(first,)
            )
        )
    ]
    scripts["resolve_approval"] = [
        _completed(
            ApprovalResolved(
                _PROJECT,
                "chat-1",
                "root-1",
                "card-1",
                "allow_for_conversation",
                True,
            )
        ),
        _completed(ApprovalResolved(_PROJECT, "chat-1", "root-1", "card-2", "deny", True)),
    ]
    scripts["wait_turn"] = [
        _completed(
            _unsettled_turn(
                "chat-1", "root-1", prompt, "approval_required", approval_cards=(second,)
            )
        )
    ]
    driver = FakeBrowserDriver("session-1", _ORIGIN, scripts)

    result = asyncio.run(_executor(driver).run(scenario, _config(tmp_path, source_db)))

    assert result.terminal_reason == "policy_exceeded"
    decisions = [call.arguments[4] for call in driver.calls if call.operation == "resolve_approval"]
    assert decisions == ["allow_for_conversation", "deny"]
    assert [call.operation for call in driver.calls].count("wait_turn") == 1
    assert [call.operation for call in driver.calls][-1] == "detach"
    driver.assert_consumed()


def test_approval_budget_is_cumulative_across_separate_turns(tmp_path: Path) -> None:
    scenario = _minimal_scenario(
        approval_policy=ApprovalPolicy("allow_for_conversation", 1)
    )
    first_turn = Turn("main", "first", "first prompt")
    second_turn = Turn("main", "second", "second prompt")
    checkpoint = dict(scenario.checkpoints[0])
    checkpoint["after_turn_id"] = "second"
    scenario = replace(
        scenario,
        construction=(first_turn, second_turn),
        checkpoints=(checkpoint,),
    )
    source_db = tmp_path / "operon.db"
    _seed_db(source_db, artifact=True)
    first_card = ApprovalCard("card-1", "fingerprint-1", "Run one", "tool")
    second_card = ApprovalCard("card-2", "fingerprint-2", "Run two", "tool")
    scripts = _base_scripts()
    scripts["submit_turn_wait"] = [
        _completed(
            _unsettled_turn(
                "chat-1",
                "root-1",
                first_turn.prompt,
                "approval_required",
                approval_cards=(first_card,),
            )
        ),
        _completed(
            _unsettled_turn(
                "chat-1",
                "root-1",
                second_turn.prompt,
                "approval_required",
                approval_cards=(second_card,),
                root_created=False,
            )
        ),
    ]
    scripts["resolve_approval"] = [
        _completed(
            ApprovalResolved(
                _PROJECT,
                "chat-1",
                "root-1",
                "card-1",
                "allow_for_conversation",
                True,
            )
        ),
        _completed(
            ApprovalResolved(
                _PROJECT,
                "chat-1",
                "root-1",
                "card-2",
                "deny",
                True,
            )
        ),
    ]
    scripts["wait_turn"] = [
        _completed(
            _settled_turn(
                "chat-1",
                "root-1",
                first_turn.prompt,
                1,
                root_created=True,
            )
        )
    ]
    driver = FakeBrowserDriver("session-1", _ORIGIN, scripts)

    result = asyncio.run(_executor(driver).run(scenario, _config(tmp_path, source_db)))

    decisions = [call.arguments[4] for call in driver.calls if call.operation == "resolve_approval"]
    assert decisions == ["allow_for_conversation", "deny"]
    assert result.terminal_reason == "policy_exceeded"
    assert [call.operation for call in driver.calls].count("submit_turn_wait") == 2
    assert [call.operation for call in driver.calls].count("wait_turn") == 1
    driver.assert_consumed()


class _RaisingDriver(FakeBrowserDriver):
    def __init__(self, failure: BaseException, scripts) -> None:
        super().__init__("session-1", _ORIGIN, scripts)
        self.failure = failure

    def submit_turn_wait(self, *args, **kwargs):
        self._calls.append(DriverCall("submit_turn_wait", tuple(args), dict(kwargs)))
        raise self.failure


@pytest.mark.parametrize("failure", [RuntimeError("boom"), asyncio.CancelledError()])
def test_exception_and_cancellation_finalize_evidence_and_detach(
    tmp_path: Path, failure: BaseException
) -> None:
    scenario = _minimal_scenario()
    source_db = tmp_path / "operon.db"
    _seed_db(source_db, artifact=True)
    scripts = _base_scripts()
    driver = _RaisingDriver(failure, scripts)

    with pytest.raises(type(failure)):
        asyncio.run(_executor(driver).run(scenario, _config(tmp_path, source_db)))

    assert [call.operation for call in driver.calls].count("detach") == 1
    manifest_path = tmp_path / "run" / "episode_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    expected = "cancelled" if isinstance(failure, asyncio.CancelledError) else "exception"
    assert manifest["terminal_reason"] == expected
    assert manifest["final_snapshot"] is not None
    driver.assert_consumed()


def test_input_required_turn_captures_persisted_input_request(tmp_path: Path) -> None:
    scenario = _minimal_scenario()
    source_db = tmp_path / "operon.db"
    _seed_db(source_db, artifact=True)
    scripts = _base_scripts()
    scripts["submit_turn_wait"] = [
        _completed(
            _unsettled_turn(
                "chat-1", "root-1", scenario.construction[0].prompt, "input_required"
            )
        )
    ]
    driver = FakeBrowserDriver("session-1", _ORIGIN, scripts)

    result = asyncio.run(_executor(driver).run(scenario, _config(tmp_path, source_db)))

    # the browser-observed terminal state stays authoritative; the DB read is supplementary capture.
    assert result.terminal_reason == "terminal_observation"
    turn = json.loads(result.manifest_path.read_text())["turns"][0]
    assert turn["persisted_response"] is None
    assert turn["persisted_input_request"] is not None
    assert turn["persisted_input_request"]["assistant_text"] == "I found a version conflict."
    assert turn["persisted_input_request"]["root_status"] == "awaiting_user_response"
    assert [call.operation for call in driver.calls] == [
        "attach",
        "new_chat",
        "submit_turn_wait",
        "detach",
    ]
    driver.assert_consumed()


def _sibling_offer_scenario() -> Scenario:
    return replace(
        _minimal_scenario(),
        response_rules=(
            ResponseRule(
                "decline-sibling-regen",
                "construction",
                "offer_to_regenerate_siblings",
                "Leave the sibling branch unchanged.",
            ),
        ),
    )


def test_prose_policy_halts_on_unresolved_scientific_question(tmp_path: Path) -> None:
    scenario = _sibling_offer_scenario()
    source_db = tmp_path / "operon.db"
    _seed_db(source_db, artifact=True)
    scripts = _base_scripts()
    scripts["submit_turn_wait"] = [
        _completed(
            _settled_turn("chat-1", "root-1", "construct artifact", 1, root_created=True)
        )
    ]
    driver = FakeBrowserDriver("session-1", _ORIGIN, scripts)
    # an excerpt absent from the settled prose leaves the reading unauditable, so the policy halts.
    interpreter = _FixedInterpreter(
        ProseInterpretationResult("scientific_caveat_only", None, "an excerpt not in the prose")
    )

    result = asyncio.run(
        EpisodeExecutor(driver, _FakeResponseReader(), interpreter).run(
            scenario, _config(tmp_path, source_db)
        )
    )

    assert result.terminal_reason == "prose_requires_review"
    operations = [call.operation for call in driver.calls]
    assert operations.count("submit_turn_wait") == 1  # halted before the trial turn
    assert "inspect_chat" not in operations  # the attended path reads DB prose, not the transcript
    assert operations[-1] == "detach"
    turn = json.loads(result.manifest_path.read_text())["turns"][0]
    assert turn["persisted_response"]["assistant_text"] == "The requested work is complete."
    driver.assert_consumed()


def test_prose_policy_submits_canonical_reply_for_offered_correction(tmp_path: Path) -> None:
    scenario = _sibling_offer_scenario()
    source_db = tmp_path / "operon.db"
    _seed_db(source_db, artifact=True)
    prose = "The sibling panels still use prior QC. Would you like me to regenerate them?"
    scripts = _base_scripts()
    scripts["submit_turn_wait"] = [
        _completed(
            _settled_turn("chat-1", "root-1", "construct artifact", 1, root_created=True)
        ),
        _completed(
            _settled_turn(
                "chat-1", "root-1", "Leave the sibling branch unchanged.", 2, root_created=False
            )
        ),
        _completed(_settled_turn("chat-1", "root-1", "run trial", 3, root_created=False)),
    ]
    driver = FakeBrowserDriver("session-1", _ORIGIN, scripts)
    reader = _FakeResponseReader({"user-turn-1": prose})
    interpreter = _FixedInterpreter(
        ProseInterpretationResult(
            "lineage_correction_offered", "decline-sibling-regen", "Would you like me to"
        )
    )

    result = asyncio.run(
        EpisodeExecutor(driver, reader, interpreter).run(scenario, _config(tmp_path, source_db))
    )

    assert result.terminal_reason == "completed"
    prompts = [call.arguments[3] for call in driver.calls if call.operation == "submit_turn_wait"]
    assert prompts == [
        "construct artifact",
        "Leave the sibling branch unchanged.",
        "run trial",
    ]
    manifest = json.loads(result.manifest_path.read_text())
    assert [item["op"] for item in manifest["steps"] if item["op"] == "response_rule"] == [
        "response_rule"
    ]
    turn_ids = [item["turn_id"] for item in manifest["turns"]]
    assert "rule.decline-sibling-regen" in turn_ids
    driver.assert_consumed()


def test_boundary_error_is_folded_into_step_evidence(tmp_path: Path) -> None:
    scenario = _minimal_scenario()
    source_db = tmp_path / "operon.db"
    _seed_db(source_db, artifact=True)
    scripts = {
        "attach": [_completed(SessionInspection(True, _ORIGIN, True))],
        "new_chat": [
            Outcome(
                "not_started",
                None,
                BrowserError(
                    "NEW_CHAT_CONTROL_UNAVAILABLE",
                    "boundary failure",
                    True,
                    {"phase": "new_chat"},
                ),
                _TIMING,
            )
        ],
        "detach": [_completed(Detached(True))],
    }
    driver = FakeBrowserDriver("session-1", _ORIGIN, scripts)

    result = asyncio.run(_executor(driver).run(scenario, _config(tmp_path, source_db)))

    assert result.terminal_reason == "chat_not_started"
    manifest = json.loads(result.manifest_path.read_text())
    chat_step = next(step for step in manifest["steps"] if step["op"] == "new_chat")
    assert chat_step["error"] == {
        "code": "NEW_CHAT_CONTROL_UNAVAILABLE",
        "retryable": True,
        "evidence": {"phase": "new_chat"},
    }
    driver.assert_consumed()
