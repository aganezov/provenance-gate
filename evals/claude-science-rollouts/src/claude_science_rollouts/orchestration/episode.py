"""Episode executor connecting compiled scenarios to runtime operations and evidence."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_science_rollouts.capture.evidence import EpisodeEvidence, file_sha256
from claude_science_rollouts.oracle.snapshot import open_readonly
from claude_science_rollouts.persistence.snapshots import (
    SnapshotBarrierConfig,
    StableSnapshot,
    await_stable_project_snapshot,
)
from claude_science_rollouts.scenario.checkpoints import evaluate_checkpoints
from claude_science_rollouts.scenario.compiler import Step, compile_scenario
from claude_science_rollouts.scenario.spec import ResponseRule, Scenario

from .driver import BrowserDriver
from .models import ChatObservation, Outcome
from .turn import (
    TurnApprovalBudget,
    TurnApprovalPolicy,
    TurnExecution,
    TurnRequest,
    run_turn,
)


@dataclass(frozen=True, slots=True)
class EpisodeConfig:
    episode_id: str
    project_id: str
    source_db: Path
    run_dir: Path
    fixture_path: Path | None = None
    trial_variant: str = "bare"
    deadline_ms: int = 120_000
    snapshot: SnapshotBarrierConfig = SnapshotBarrierConfig()

    def __post_init__(self) -> None:
        if not self.episode_id or not self.project_id:
            raise ValueError("episode_id and project_id must be non-empty")
        if (
            isinstance(self.deadline_ms, bool)
            or not isinstance(self.deadline_ms, int)
            or self.deadline_ms <= 0
        ):
            raise ValueError("deadline_ms must be a positive integer")


@dataclass(frozen=True, slots=True)
class EpisodeResult:
    terminal_reason: str
    manifest_path: Path
    final_snapshot: Path | None
    detach_outcome: str | None


class _EpisodeStop(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def approval_policy_for_scenario(scenario: Scenario) -> TurnApprovalPolicy:
    """Map validated scenario policy into the runtime-owned approval bound."""
    return TurnApprovalPolicy(
        scenario.approval_policy.action,
        scenario.approval_policy.max_approvals,
    )


def _prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()


def _outcome_reason(prefix: str, outcome: Outcome[Any]) -> str:
    if outcome.outcome == "completed":
        return f"{prefix}_invalid_result"
    return f"{prefix}_{outcome.outcome}"


def _require_completed(prefix: str, outcome: Outcome[Any]) -> Any:
    if not outcome.completed:
        raise _EpisodeStop(_outcome_reason(prefix, outcome))
    assert outcome.result is not None
    return outcome.result


def matches_response_rule(rule: ResponseRule, observation: ChatObservation) -> bool:
    if rule.trigger != "offer_to_regenerate_siblings":
        raise ValueError(f"unsupported deterministic response-rule trigger: {rule.trigger}")
    latest_assistant = next(
        (turn for turn in reversed(observation.transcript) if turn.role == "assistant"),
        None,
    )
    if latest_assistant is None or latest_assistant.truncated:
        return False
    assistant_text = latest_assistant.text.lower()
    branch_terms = ("sibling", "composition", "cytotoxic", "qc summary", "panel")
    return "regenerate" in assistant_text and any(term in assistant_text for term in branch_terms)


def _turn_record(turn_id: str, execution: TurnExecution) -> dict[str, Any]:
    final = execution.final
    result = final.result
    delivery = result.delivery if result is not None else None
    return {
        "turn_id": turn_id,
        "outcome": final.outcome,
        "stop_reason": execution.stop_reason,
        "turn_state": result.turn_state if result else None,
        "project_id": result.project_id if result else None,
        "chat_id": result.chat_id if result else None,
        "root_frame_id": result.root_frame_id if result else None,
        "authored_prompt_sha256": (
            delivery.authored_prompt_sha256 if delivery is not None else None
        ),
        "delivery_text_sha256": (
            delivery.delivery_text_sha256 if delivery is not None else None
        ),
        "normalized_user_turn_id": (
            delivery.normalized_user_turn_id if delivery is not None else None
        ),
        "approvals": [
            {
                "outcome": resolution.outcome,
                "card_id": resolution.result.card_id if resolution.result else None,
                "decision": resolution.result.decision if resolution.result else None,
            }
            for resolution in execution.approval_resolutions
        ],
        "wait_count": execution.wait_count,
    }


def _turn_stop_reason(execution: TurnExecution) -> str | None:
    if execution.final.outcome != "completed":
        return f"turn_{execution.final.outcome}"
    if execution.stop_reason != "settled":
        return execution.stop_reason
    return None


class EpisodeExecutor:
    """Execute one compiled scenario and own its finalization boundary."""

    def __init__(self, driver: BrowserDriver) -> None:
        self.driver = driver

    async def run(self, scenario: Scenario, config: EpisodeConfig) -> EpisodeResult:
        evidence = EpisodeEvidence(scenario.scenario_id, config.project_id)
        plan = compile_scenario(scenario, trial=config.trial_variant)
        policy = approval_policy_for_scenario(scenario)
        approval_budget = TurnApprovalBudget.from_policy(policy)
        chats: dict[str, str] = {}
        roots: dict[str, str] = {}
        checkpoint_by_id = {item["id"]: item for item in scenario.checkpoints}
        rules_after: dict[str, list[ResponseRule]] = {}
        for rule in scenario.response_rules:
            rules_after.setdefault(rule.after_turn_id, []).append(rule)

        terminal_reason = "completed"
        pending_error: BaseException | None = None
        final_snapshot: StableSnapshot | None = None
        manifest_path: Path | None = None
        detach_outcome: str | None = None
        try:
            attached = self.driver.attach(
                request_id=f"{config.episode_id}.attach",
                deadline_ms=config.deadline_ms,
            )
            inspection = _require_completed("attach", attached)
            if not inspection.authenticated or not inspection.profile_ready:
                raise _EpisodeStop("session_not_ready")
            if inspection.origin != self.driver.origin:
                raise _EpisodeStop("session_origin_mismatch")
            await self._execute_plan(
                scenario,
                plan,
                config,
                approval_budget,
                chats,
                roots,
                checkpoint_by_id,
                rules_after,
                evidence,
            )
        except _EpisodeStop as exc:
            terminal_reason = exc.reason
        except BaseException as exc:
            terminal_reason = (
                "cancelled" if isinstance(exc, asyncio.CancelledError) else "exception"
            )
            evidence.record_exception(exc)
            pending_error = exc
        finally:
            try:
                final_snapshot = await await_stable_project_snapshot(
                    config.source_db,
                    config.project_id,
                    config.run_dir,
                    config=config.snapshot,
                )
                evidence.record_snapshot(
                    final_snapshot.path,
                    config.run_dir,
                    final_snapshot.attempts,
                )
            except BaseException as exc:
                if pending_error is None:
                    pending_error = exc
                    terminal_reason = "finalization_failed"
                    evidence.record_exception(exc)
            try:
                detached = self.driver.detach(
                    request_id=f"{config.episode_id}.detach",
                    deadline_ms=config.deadline_ms,
                )
                detach_outcome = detached.outcome
                evidence.steps.append(
                    {
                        "index": len(evidence.steps),
                        "op": "detach",
                        "outcome": detached.outcome,
                        "detached": (
                            detached.result.detached if detached.result is not None else None
                        ),
                    }
                )
                if pending_error is None:
                    if not detached.completed:
                        terminal_reason = f"detach_{detached.outcome}"
                    elif detached.result is None or not detached.result.detached:
                        terminal_reason = "detach_failed"
            except BaseException as exc:
                if pending_error is None:
                    pending_error = exc
                    terminal_reason = "detach_exception"
                    evidence.record_exception(exc)
            evidence.finish(terminal_reason)
            try:
                manifest_path = evidence.write(config.run_dir)
            except BaseException as exc:
                if pending_error is None:
                    pending_error = exc

        if pending_error is not None:
            raise pending_error
        assert manifest_path is not None
        return EpisodeResult(
            terminal_reason,
            manifest_path,
            final_snapshot.path if final_snapshot else None,
            detach_outcome,
        )

    async def _execute_plan(
        self,
        scenario: Scenario,
        plan: tuple[Step, ...],
        config: EpisodeConfig,
        approval_budget: TurnApprovalBudget,
        chats: dict[str, str],
        roots: dict[str, str],
        checkpoint_by_id: dict[str, dict[str, Any]],
        rules_after: dict[str, list[ResponseRule]],
        evidence: EpisodeEvidence,
    ) -> None:
        for step in plan:
            evidence.steps.append(
                {
                    "index": len(evidence.steps),
                    "op": step.op,
                    "session": step.session,
                    "turn_id": step.turn_id,
                    "checkpoint_id": step.checkpoint_id,
                }
            )
            if step.op in {"new_chat", "open_chat"}:
                self._focus_chat(step, config, chats)
            elif step.op == "attach":
                self._upload_fixture(step, config, chats)
            elif step.op == "submit":
                assert step.session and step.turn_id and step.prompt is not None
                execution = self._run_turn(
                    step.session,
                    step.turn_id,
                    step.prompt,
                    config,
                    approval_budget,
                    chats,
                    roots,
                )
                evidence.turns.append(_turn_record(step.turn_id, execution))
                stop = _turn_stop_reason(execution)
                if stop:
                    raise _EpisodeStop(stop)
                result = execution.final.result
                assert result is not None
                roots[step.session] = result.root_frame_id
                for rule in rules_after.get(step.turn_id, []):
                    self._apply_response_rule(
                        rule,
                        step.session,
                        config,
                        approval_budget,
                        chats,
                        roots,
                        evidence,
                    )
            elif step.op == "gate":
                assert step.checkpoint_id
                await self._evaluate_gate(
                    checkpoint_by_id[step.checkpoint_id], config, evidence
                )
            else:
                raise ValueError(f"unsupported compiled step: {step.op}")

    def _focus_chat(
        self, step: Step, config: EpisodeConfig, chats: dict[str, str]
    ) -> None:
        assert step.session
        request_id = f"{config.episode_id}.chat.{step.session}"
        if step.op == "new_chat":
            outcome = self.driver.new_chat(
                config.project_id,
                request_id=request_id,
                deadline_ms=config.deadline_ms,
            )
        else:
            chat_id = chats.get(step.session)
            if chat_id is None:
                raise _EpisodeStop(f"missing_chat_{step.session}")
            outcome = self.driver.open_chat(
                config.project_id,
                chat_id,
                request_id=request_id,
                deadline_ms=config.deadline_ms,
            )
        observation = _require_completed("chat", outcome)
        if observation.project_id != config.project_id:
            raise _EpisodeStop("chat_project_mismatch")
        if step.op == "new_chat" and (
            not observation.composer_empty
            or observation.user_turn_count != 0
            or observation.root_frame_id is not None
        ):
            raise _EpisodeStop("new_chat_not_fresh")
        chats[step.session] = observation.chat_id

    def _upload_fixture(
        self, step: Step, config: EpisodeConfig, chats: dict[str, str]
    ) -> None:
        assert step.session
        if config.fixture_path is None:
            raise _EpisodeStop("fixture_path_missing")
        assert step.fixture is not None
        expected_filename = step.fixture["filename"]
        expected_sha256 = step.fixture["sha256"]
        if config.fixture_path.name != expected_filename:
            raise _EpisodeStop("fixture_filename_mismatch")
        if file_sha256(config.fixture_path) != expected_sha256:
            raise _EpisodeStop("fixture_sha256_mismatch")
        outcome = self.driver.upload_attachment(
            config.project_id,
            chats[step.session],
            config.fixture_path,
            request_id=f"{config.episode_id}.fixture",
            deadline_ms=config.deadline_ms,
        )
        accepted = _require_completed("attachment", outcome)
        if (
            accepted.project_id != config.project_id
            or accepted.chat_id != chats[step.session]
            or accepted.filename != expected_filename
        ):
            raise _EpisodeStop("attachment_identity_mismatch")
        if not accepted.accepted:
            raise _EpisodeStop("attachment_not_accepted")

    def _run_turn(
        self,
        session: str,
        turn_id: str,
        prompt: str,
        config: EpisodeConfig,
        approval_budget: TurnApprovalBudget,
        chats: dict[str, str],
        roots: dict[str, str],
    ) -> TurnExecution:
        root = roots.get(session)
        return run_turn(
            self.driver,
            TurnRequest(
                project_id=config.project_id,
                chat_id=chats[session],
                root_mode="existing" if root else "new",
                prompt=prompt,
                authored_prompt_sha256=_prompt_sha256(prompt),
                request_id_prefix=f"{config.episode_id}.{turn_id}",
                deadline_ms=config.deadline_ms,
                root_frame_id=root,
            ),
            approval_budget=approval_budget,
        )

    def _apply_response_rule(
        self,
        rule: ResponseRule,
        session: str,
        config: EpisodeConfig,
        approval_budget: TurnApprovalBudget,
        chats: dict[str, str],
        roots: dict[str, str],
        evidence: EpisodeEvidence,
    ) -> None:
        observed = self.driver.inspect_chat(
            config.project_id,
            chats[session],
            request_id=f"{config.episode_id}.rule.{rule.id}.inspect",
            deadline_ms=config.deadline_ms,
            root_frame_id=roots[session],
        )
        observation = _require_completed("response_rule_inspect", observed)
        if (
            observation.project_id != config.project_id
            or observation.chat_id != chats[session]
            or observation.root_frame_id != roots[session]
        ):
            raise _EpisodeStop("response_rule_identity_mismatch")
        if not matches_response_rule(rule, observation):
            return
        execution = self._run_turn(
            session,
            f"rule.{rule.id}",
            rule.reply,
            config,
            approval_budget,
            chats,
            roots,
        )
        evidence.steps.append(
            {
                "index": len(evidence.steps),
                "op": "response_rule",
                "session": session,
                "turn_id": f"rule.{rule.id}",
                "checkpoint_id": None,
            }
        )
        evidence.turns.append(_turn_record(f"rule.{rule.id}", execution))
        stop = _turn_stop_reason(execution)
        if stop:
            raise _EpisodeStop(stop)
        result = execution.final.result
        assert result is not None
        roots[session] = result.root_frame_id

    async def _evaluate_gate(
        self,
        checkpoint: dict[str, Any],
        config: EpisodeConfig,
        evidence: EpisodeEvidence,
    ) -> None:
        work_root = config.run_dir / ".checkpoint-work"
        work_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work_root) as temporary:
            stable = await await_stable_project_snapshot(
                config.source_db,
                config.project_id,
                Path(temporary),
                config=config.snapshot,
            )
            conn = open_readonly(stable.path)
            try:
                result = evaluate_checkpoints(conn, config.project_id, [checkpoint])[0]
            finally:
                conn.close()
        if work_root.exists() and not any(work_root.iterdir()):
            work_root.rmdir()
        evidence.checkpoints.append(
            {
                "id": result.id,
                "mode": result.mode,
                "passed": result.passed,
                "stability_attempts": stable.attempts,
                "assertions": [
                    {"kind": assertion.kind, "ok": assertion.ok, "detail": assertion.detail}
                    for assertion in result.assertions
                ],
            }
        )
        if result.mode == "gate" and not result.passed:
            raise _EpisodeStop(f"checkpoint_failed_{result.id}")
