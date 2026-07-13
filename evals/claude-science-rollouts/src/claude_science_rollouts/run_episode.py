"""Run one scenario-driven live Claude Science rollout and capture its evidence.

This is the harness's generation entry point. It stands up a fresh Claude Science project through
the browser boundary, pins the run to an accepted baseline (enabled skills, agent context, and the
selected model), drives the compiled scenario to a terminal shape, and freezes the resulting operon
evidence into an externally owned run directory.

It is generation-only: it captures the manifest, the reduced project snapshot, and the terminal
prose or input request the rollout ended on, then stops. It never scores the provenance graph — the
provenance gate consumes the captured snapshot and forms its own verdict. The only fail-closed stops
here are about generation integrity (an untrusted baseline, a broken drive, or an unusable capture),
never about the scientific outcome the rollout produced.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from claude_science_rollouts.browser import BrowserBridge, BrowserClient, BrowserSession
from claude_science_rollouts.capture.evidence import file_sha256
from claude_science_rollouts.oracle.snapshot import open_readonly
from claude_science_rollouts.orchestration.browser_driver import TypedBrowserDriver
from claude_science_rollouts.orchestration.episode import EpisodeConfig, EpisodeExecutor
from claude_science_rollouts.orchestration.models import bounded_label
from claude_science_rollouts.orchestration.prose import SubprocessProseInterpreter
from claude_science_rollouts.persistence.responses import DatabaseResponseReader
from claude_science_rollouts.persistence.snapshots import SnapshotBarrierConfig
from claude_science_rollouts.scenario import Scenario, load_scenario

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
_REPOSITORY_ROOT = _PACKAGE_ROOT.parents[1]
_BROWSER_ROOT = _PACKAGE_ROOT / "browser"
_BOUNDARY_MAIN = _BROWSER_ROOT / "src" / "main.mjs"

# a captured rollout is "operationally complete" when the drive reached a terminal shape the frozen
# database proved, whether that was terminal prose or a paused input request. Any other reason is a
# mechanical stop. Both are recorded, never graded — this only classifies the capture for consumers.
_OPERATIONAL_TERMINAL_REASONS = frozenset({"completed", "terminal_observation"})


class RunFailure(RuntimeError):
    """A precise fail-closed generation stop, safe to report without raw browser output."""


def sorted_set_sha256(values: frozenset[str]) -> str:
    """Hash lexically sorted unique values joined by newlines, without a trailing newline."""
    canonical = "\n".join(sorted(values))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _episode_id(scenario_id: str) -> str:
    """A collision-resistant, filesystem-safe id for one run directory."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    scenario_hash = hashlib.sha256(scenario_id.encode()).hexdigest()[:10]
    slug = re.sub(r"[^A-Za-z0-9._:-]+", "-", scenario_id).strip("-._:")
    slug = slug[:48].rstrip("-._:") or "scenario"
    return f"{slug}-{scenario_hash}-{timestamp}-{uuid.uuid4().hex[:10]}"


def _driver(args: argparse.Namespace) -> TypedBrowserDriver:
    """Build one typed browser driver over an externally owned, non-symlinked state directory."""
    node = shutil.which("node")
    if node is None:
        raise RunFailure("Node is unavailable")
    if not _BOUNDARY_MAIN.is_file():
        raise RunFailure("browser boundary entrypoint is unavailable")
    state_value = getattr(args, "browser_state_dir", None)
    if state_value is None:
        raise RunFailure("external browser state directory is required")
    state_dir = Path(state_value).resolve()
    repository = _REPOSITORY_ROOT.resolve()
    if state_dir == repository or state_dir.is_relative_to(repository):
        raise RunFailure("browser state directory must be external to the repository")
    if Path(state_value).is_symlink():
        raise RunFailure("browser state directory cannot be a symlink")
    state_dir.mkdir(parents=True, exist_ok=True)
    os.environ["BROWSER_OWNER_NAME"] = args.browser_owner
    bridge = BrowserBridge((node, str(_BOUNDARY_MAIN)), cwd=state_dir)
    client = BrowserClient(bridge, args.session_id, args.origin)
    return TypedBrowserDriver(BrowserSession(client))


def _require_completed(outcome: Any, operation: str) -> Any:
    """Lift a boundary outcome to its result, or fail closed with a sanitized error code."""
    if not outcome.completed:
        code = outcome.error.code if outcome.error is not None else "MISSING_ERROR"
        raise RunFailure(f"{operation} {outcome.outcome}: {code}")
    if outcome.result is None:
        raise RunFailure(f"{operation} completed without a result")
    return outcome.result


def _detach(driver: TypedBrowserDriver, request_id: str, deadline_ms: int) -> str:
    outcome = driver.detach(request_id=request_id, deadline_ms=deadline_ms)
    if not outcome.completed or outcome.result is None or not outcome.result.detached:
        code = outcome.error.code if outcome.error is not None else "DETACH_UNCONFIRMED"
        raise RunFailure(f"detach {outcome.outcome}: {code}")
    return outcome.outcome


def _external_run_dir(run_root: Path, episode_id: str) -> Path:
    """Create a unique run directory strictly outside the repository."""
    root = run_root.resolve()
    repository = _REPOSITORY_ROOT.resolve()
    if root == repository or root.is_relative_to(repository):
        raise RunFailure("run root must be external to the repository")
    if run_root.is_symlink():
        raise RunFailure("run root cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / episode_id
    if run_dir.exists() or run_dir.is_symlink():
        raise RunFailure("unique run directory already exists")
    run_dir.mkdir()
    return run_dir


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    """Atomically write a canonical JSON record, refusing to replace an existing file."""
    if path.exists() or path.is_symlink():
        raise RunFailure(f"refusing to replace existing output: {path.name}")
    temporary = path.with_name(f".{path.name}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise RunFailure(f"temporary output already exists: {temporary.name}")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    temporary.replace(path)
    return path


def _pin_model(
    driver: TypedBrowserDriver,
    args: argparse.Namespace,
    episode_id: str,
    project_id: str,
) -> dict[str, Any]:
    """Select and confirm the requested model on a fresh blank chat, then verify the pin held.

    The model picker is set on a verified blank chat so Claude Science carries it as the project's
    selection for the rollout that follows. The expected model *identifier* cannot be read back from
    the picker; it is captured as run metadata and later borne out by each turn's persisted
    ``root_model_identifier`` in the manifest.
    """
    chat = _require_completed(
        driver.new_chat(
            project_id,
            request_id=f"{episode_id}.preflight.chat",
            deadline_ms=args.deadline_ms,
        ),
        "preflight chat",
    )
    if (
        chat.project_id != project_id
        or not chat.composer_empty
        or chat.user_turn_count != 0
        or chat.root_frame_id is not None
    ):
        raise RunFailure("preflight chat is not verified fresh")
    selection = _require_completed(
        driver.select_model(
            project_id,
            chat.chat_id,
            args.model_label,
            request_id=f"{episode_id}.preflight.model",
            deadline_ms=args.deadline_ms,
        ),
        "model selection",
    )
    if not selection.confirmed:
        raise RunFailure("model selection was not confirmed")
    if selection.model_label != args.model_label:
        raise RunFailure("selected model does not match the requested label")
    return {
        "model_label": selection.model_label,
        "previous_model_label": selection.previous_model_label,
        "changed": selection.changed,
        "confirmed": selection.confirmed,
    }


def _fresh_project_preflight(
    args: argparse.Namespace,
    episode_id: str,
    project_name: str,
    run_dir: Path,
) -> tuple[str, dict[str, Any]]:
    """Create a fresh project, verify it against the accepted baseline, and pin the model.

    Every check here guards generation integrity before a live rollout is spent: the session is
    ready at the required origin, the created project is verified fresh and rootless, the enabled
    skills and agent context match the accepted baseline exactly, and the requested model is pinned
    and confirmed. Its own attach/detach lifecycle keeps this preflight independent of the drive.
    """
    driver = _driver(args)
    attached = False
    detach_outcome: str | None = None
    project_id: str | None = None
    baseline: dict[str, Any] | None = None
    failure: BaseException | None = None
    detach_failure: BaseException | None = None
    try:
        inspection = _require_completed(
            driver.attach(
                request_id=f"{episode_id}.preflight.attach",
                deadline_ms=args.deadline_ms,
            ),
            "preflight attach",
        )
        attached = True
        if (
            not inspection.authenticated
            or not inspection.profile_ready
            or inspection.origin != args.origin
        ):
            raise RunFailure("preflight session is not ready at the required origin")
        created = _require_completed(
            driver.create_project(
                project_name,
                request_id=f"{episode_id}.preflight.create",
                deadline_ms=args.deadline_ms,
            ),
            "project creation",
        )
        project_id = created.project_id
        observed = _require_completed(
            driver.inspect_project(
                project_id,
                request_id=f"{episode_id}.preflight.project",
                deadline_ms=args.deadline_ms,
            ),
            "project inspection",
        )
        if created != observed:
            raise RunFailure("fresh project observation drifted after creation")
        if (
            not observed.verified
            or not observed.composer_empty
            or observed.user_turn_count != 0
            or observed.root_frame_id is not None
            or observed.root_state is not None
        ):
            raise RunFailure("created project is not verified fresh and rootless")
        context = _require_completed(
            driver.inspect_context(
                project_id,
                request_id=f"{episode_id}.preflight.context",
                deadline_ms=args.deadline_ms,
            ),
            "context inspection",
        )
        if context.project_id != project_id:
            raise RunFailure("context inspection project identity mismatch")
        skill_hash = sorted_set_sha256(context.enabled_skills)
        if len(context.enabled_skills) != args.expected_skill_count:
            raise RunFailure("enabled-skill count does not match the accepted baseline")
        if skill_hash != args.expected_skill_hash:
            raise RunFailure("enabled-skill set hash does not match the accepted baseline")
        if context.context_hash != args.expected_context_hash:
            raise RunFailure("context hash does not match the accepted baseline")
        model_selection = _pin_model(driver, args, episode_id, project_id)
        baseline = {
            "enabled_skill_count": len(context.enabled_skills),
            "enabled_skill_set_sha256": skill_hash,
            "context_sha256": context.context_hash,
            "model_selection": model_selection,
        }
    except BaseException as exc:
        failure = exc
    finally:
        if attached:
            try:
                detach_outcome = _detach(
                    driver, f"{episode_id}.preflight.detach", args.deadline_ms
                )
            except BaseException as exc:
                detach_failure = exc
                if failure is None:
                    failure = exc
    record = {
        "episode_id": episode_id,
        "project_id": project_id,
        "baseline": baseline,
        "detach_outcome": detach_outcome,
        "failure": type(failure).__name__ if failure is not None else None,
        "detach_failure": (type(detach_failure).__name__ if detach_failure is not None else None),
    }
    _write_json(run_dir / "preflight.json", record)
    if failure is not None:
        raise failure
    assert project_id is not None and baseline is not None
    return project_id, baseline


def _validate_inputs(args: argparse.Namespace) -> tuple[Path, Path]:
    """Resolve the scenario file and the external source database, or fail closed."""
    scenario = args.scenario.resolve()
    source_db = args.source_db.resolve()
    if not scenario.is_file():
        raise RunFailure("scenario file is unavailable")
    if not source_db.is_file():
        raise RunFailure("source database is unavailable")
    if source_db.is_relative_to(_REPOSITORY_ROOT.resolve()):
        raise RunFailure("source database must be external to the repository")
    return scenario, source_db


def _validate_bounded_text(value: object, label: str) -> None:
    if not bounded_label(value):
        raise RunFailure(f"{label} must be bounded non-empty text")


def _validate_run_parameters(args: argparse.Namespace) -> None:
    """Reject malformed run parameters before any browser or database work begins."""
    _validate_bounded_text(args.model_label, "model label")
    _validate_bounded_text(args.expected_model_identifier, "expected model identifier")
    _validate_bounded_text(args.browser_owner, "browser owner")
    if args.expected_skill_count < 0:
        raise RunFailure("expected skill count cannot be negative")
    for name in ("expected_skill_hash", "expected_context_hash"):
        value = getattr(args, name)
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise RunFailure(f"{name.replace('_', ' ')} must be a lowercase SHA-256 digest")
    timing = (args.snapshot_poll_seconds, args.snapshot_timeout_seconds)
    if (
        any(not math.isfinite(value) for value in timing)
        or args.snapshot_poll_seconds < 0
        or args.snapshot_timeout_seconds <= 0
    ):
        raise RunFailure("snapshot timing values are invalid")
    if (
        not math.isfinite(args.prose_interpreter_timeout_seconds)
        or args.prose_interpreter_timeout_seconds <= 0
        or args.prose_interpreter_timeout_seconds > 300
    ):
        raise RunFailure("prose interpreter timeout must be in (0, 300] seconds")


def _resolve_fixture(
    scenario: Scenario, scenario_path: Path, supplied_path: Path | None
) -> Path | None:
    """Resolve the scenario's declared seed fixture and verify its name and content hash."""
    fixture = scenario.fixture
    if fixture is None:
        if supplied_path is not None:
            raise RunFailure("scenario does not declare a fixture")
        return None
    fixture_path = (
        supplied_path.resolve()
        if supplied_path is not None
        else scenario_path.parent / "fixtures" / fixture["filename"]
    )
    if not fixture_path.is_file():
        raise RunFailure("scenario fixture is unavailable")
    if fixture_path.name != fixture["filename"]:
        raise RunFailure("fixture filename does not match the scenario declaration")
    if file_sha256(fixture_path) != fixture["sha256"]:
        raise RunFailure("fixture SHA-256 does not match the scenario declaration")
    return fixture_path


def _validate_trial(scenario: Scenario, trial: str) -> None:
    if trial not in scenario.trial.variants:
        raise RunFailure(f"scenario does not declare trial variant {trial!r}")


def _resolve_prose_interpreter(
    args: argparse.Namespace, run_dir: Path
) -> SubprocessProseInterpreter | None:
    """Build the optional out-of-process prose classifier over an external executable."""
    if args.prose_interpreter_command is None:
        return None
    command = args.prose_interpreter_command.resolve()
    if not command.is_file():
        raise RunFailure("prose interpreter executable is unavailable")
    if command.is_relative_to(_REPOSITORY_ROOT.resolve()):
        raise RunFailure("prose interpreter executable must be external to the repository")
    return SubprocessProseInterpreter(
        (str(command),),
        evidence_dir=run_dir / "attended-interpreter",
        timeout_seconds=args.prose_interpreter_timeout_seconds,
    )


def _verify_snapshot_capture(snapshot_path: Path, project_id: str) -> None:
    """Confirm the frozen snapshot opens read-only and holds exactly the captured project row."""
    conn = open_readonly(snapshot_path)
    try:
        rows = conn.execute(
            "SELECT id FROM projects WHERE id = ?", (project_id,)
        ).fetchall()
    finally:
        conn.close()
    if len(rows) != 1:
        raise RunFailure("final snapshot does not contain exactly the captured project")


def _outcome_classification(terminal_reason: str) -> str:
    if terminal_reason == "completed":
        return "completed"
    if terminal_reason == "terminal_observation":
        return "terminal_observation"
    return "incomplete"


def _observed_model_identifiers(turns: list[dict[str, Any]]) -> set[str]:
    """The non-null root model identifiers CS recorded across the captured turns — the evidence of
    which model each turn actually ran under. A turn that never surfaced an identifier contributes
    nothing (the persisted reader allows it to be absent)."""
    identifiers: set[str] = set()
    for turn in turns:
        for key in ("persisted_response", "persisted_input_request"):
            record = turn.get(key)
            if record and record.get("root_model_identifier"):
                identifiers.add(record["root_model_identifier"])
    return identifiers


def _run_summary(
    args: argparse.Namespace,
    scenario: Scenario,
    episode_id: str,
    project_id: str,
    baseline: dict[str, Any],
    result: Any,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the generation record: identity, baseline, capture evidence, and terminal shape."""
    assert result.final_snapshot is not None
    turns = manifest["turns"]
    final_turn = turns[-1] if turns else None
    final_prose = final_turn["persisted_response"] if final_turn is not None else None
    final_input_request = (
        final_turn.get("persisted_input_request") if final_turn is not None else None
    )
    # a rollout is only operationally complete if every turn that named a model ran under the one we
    # pinned — a picker mismatch or drift otherwise yields a "complete" record for the wrong model.
    observed_models = _observed_model_identifiers(turns)
    mismatched_models = sorted(m for m in observed_models if m != args.expected_model_identifier)
    model_identity_verified = not mismatched_models
    return {
        "episode_id": episode_id,
        "project_id": project_id,
        "scenario_id": scenario.scenario_id,
        "trial_variant": args.trial,
        "model_label": args.model_label,
        "expected_model_identifier": args.expected_model_identifier,
        "terminal_reason": result.terminal_reason,
        "outcome_classification": _outcome_classification(result.terminal_reason),
        "operationally_complete": (
            result.terminal_reason in _OPERATIONAL_TERMINAL_REASONS and model_identity_verified
        ),
        "model_identity": {
            "expected": args.expected_model_identifier,
            "observed": sorted(observed_models),
            "mismatched": mismatched_models,
            "verified": model_identity_verified,
        },
        "baseline": baseline,
        "manifest": {
            "path": str(result.manifest_path.resolve()),
            "sha256": file_sha256(result.manifest_path),
        },
        "final_snapshot": {
            "path": str(result.final_snapshot.resolve()),
            "sha256": file_sha256(result.final_snapshot),
            "size_bytes": result.final_snapshot.stat().st_size,
            "stability_attempts": manifest["final_snapshot"]["stability_attempts"],
        },
        "checkpoints": manifest["checkpoints"],
        "database_derived_final_assistant_prose": final_prose,
        "database_derived_final_input_request": final_input_request,
        "detach_outcome": result.detach_outcome,
    }


def _run_episode(args: argparse.Namespace) -> dict[str, Any]:
    """Drive one rollout end to end and return the path to its captured generation record."""
    _validate_run_parameters(args)
    scenario_path, source_db = _validate_inputs(args)
    scenario = load_scenario(scenario_path)
    _validate_trial(scenario, args.trial)
    fixture_path = _resolve_fixture(scenario, scenario_path, args.fixture)

    episode_id = _episode_id(scenario.scenario_id)
    run_dir = _external_run_dir(args.run_root, episode_id)
    args.browser_state_dir = run_dir / "browser-state"
    project_id, baseline = _fresh_project_preflight(
        args, episode_id, f"Episode {episode_id}", run_dir
    )
    interpreter = _resolve_prose_interpreter(args, run_dir)

    result = asyncio.run(
        EpisodeExecutor(
            _driver(args),
            DatabaseResponseReader(),
            prose_interpreter=interpreter,
        ).run(
            scenario,
            EpisodeConfig(
                episode_id=episode_id,
                project_id=project_id,
                source_db=source_db,
                run_dir=run_dir,
                fixture_path=fixture_path,
                trial_variant=args.trial,
                deadline_ms=args.deadline_ms,
                snapshot=SnapshotBarrierConfig(
                    poll_interval_seconds=args.snapshot_poll_seconds,
                    timeout_seconds=args.snapshot_timeout_seconds,
                ),
            ),
        )
    )
    if result.final_snapshot is None:
        raise RunFailure("episode finalized without a stable snapshot")
    _verify_snapshot_capture(result.final_snapshot, project_id)

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    summary = _run_summary(
        args, scenario, episode_id, project_id, baseline, result, manifest
    )
    summary_path = _write_json(run_dir / "run_summary.json", summary)
    return {"summary_path": str(summary_path.resolve()), **summary}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, type=Path)
    parser.add_argument("--trial", required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--model-label", required=True)
    parser.add_argument("--expected-model-identifier", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--browser-owner", required=True)
    parser.add_argument("--session-id", default="episode-integration")
    parser.add_argument("--deadline-ms", type=int, default=120_000)
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--expected-skill-count", required=True, type=int)
    parser.add_argument("--expected-skill-hash", required=True)
    parser.add_argument("--expected-context-hash", required=True)
    parser.add_argument("--snapshot-poll-seconds", type=float, default=0.5)
    parser.add_argument("--snapshot-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--prose-interpreter-command", type=Path)
    parser.add_argument("--prose-interpreter-timeout-seconds", type=float, default=120.0)
    return parser


def _fail(message: str) -> NoReturn:
    print(json.dumps({"status": "failed", "reason": message}), file=sys.stderr)
    raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.deadline_ms <= 0:
        _fail("deadline must be positive")
    try:
        payload = _run_episode(args)
    except (OSError, ValueError, RunFailure) as exc:
        _fail(str(exc))
    except Exception as exc:
        # sanitize any unexpected boundary failure into a stable, non-leaking reason.
        _fail(f"{type(exc).__name__}: run failed")
    print(json.dumps({"status": "completed", **payload}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
