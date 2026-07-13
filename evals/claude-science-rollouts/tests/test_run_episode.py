"""Offline coverage for the generation entry point's parsing and validation surface.

These tests never launch Node or a browser: they exercise the pure argument-parsing, input- and
parameter-validation, fixture/trial resolution, and snapshot-capture helpers directly. The live
``run()`` path is exercised end to end only against a real Claude Science instance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from claude_science_rollouts import run_episode
from claude_science_rollouts.run_episode import (
    RunFailure,
    _episode_id,
    _outcome_classification,
    _parser,
    _resolve_fixture,
    _validate_inputs,
    _validate_run_parameters,
    _validate_trial,
    _verify_snapshot_capture,
    main,
    sorted_set_sha256,
)
from claude_science_rollouts.scenario.spec import Scenario, Session, Trial, Turn


def _scenario(*, fixture: dict | None = None, variants: dict[str, str] | None = None) -> Scenario:
    return Scenario(
        schema_version=1,
        scenario_id="demo-scenario",
        tier="scientific",
        sessions=(Session(id="s", chat="new"),),
        construction=(Turn(session="s", turn_id="t1", prompt="hello"),),
        trial=Trial(session="s", turn_id="trial", variants=variants or {"bare": "go"}),
        checkpoints=(),
        fixture=fixture,
    )


def _params(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "model_label": "Claude Opus 4.5",
        "expected_model_identifier": "claude-opus-4-5",
        "browser_owner": "cs-harness",
        "expected_skill_count": 16,
        "expected_skill_hash": "a" * 64,
        "expected_context_hash": "b" * 64,
        "snapshot_poll_seconds": 0.5,
        "snapshot_timeout_seconds": 60.0,
        "prose_interpreter_timeout_seconds": 120.0,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _full_argv(**overrides: str) -> list[str]:
    values = {
        "--scenario": "scenario.json",
        "--trial": "bare",
        "--model-label": "Claude Opus 4.5",
        "--expected-model-identifier": "claude-opus-4-5",
        "--origin": "http://127.0.0.1:8765",
        "--browser-owner": "harness",
        "--source-db": "operon.db",
        "--run-root": "/tmp/runs",
        "--expected-skill-count": "16",
        "--expected-skill-hash": "a" * 64,
        "--expected-context-hash": "b" * 64,
    }
    values.update(overrides)
    argv: list[str] = []
    for flag, value in values.items():
        argv.extend([flag, value])
    return argv


# --- argument parsing ---------------------------------------------------------------------------


def test_parser_parses_full_argv() -> None:
    args = _parser().parse_args(_full_argv())
    assert args.scenario == Path("scenario.json")
    assert args.source_db == Path("operon.db")
    assert args.run_root == Path("/tmp/runs")
    assert args.trial == "bare"
    assert args.expected_skill_count == 16
    # defaults land where the reference set them.
    assert args.session_id == "episode-integration"
    assert args.deadline_ms == 120_000
    assert args.snapshot_poll_seconds == 0.5
    assert args.snapshot_timeout_seconds == 60.0
    assert args.prose_interpreter_command is None
    assert args.fixture is None


def test_parser_missing_required_exits() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args([])


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_main_rejects_nonpositive_deadline(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(_full_argv(**{"--deadline-ms": "0"}))
    assert excinfo.value.code == 2
    reason = json.loads(capsys.readouterr().err)
    assert reason == {"status": "failed", "reason": "deadline must be positive"}


# --- run-parameter validation -------------------------------------------------------------------


def test_validate_run_parameters_accepts_valid() -> None:
    _validate_run_parameters(_params())


@pytest.mark.parametrize(
    "overrides",
    [
        {"model_label": ""},
        {"model_label": " leading-space"},
        {"model_label": "x" * 129},
        {"model_label": "with\tcontrol"},
        {"expected_model_identifier": ""},
        {"expected_model_identifier": "trailing "},
        {"browser_owner": "bad\nowner"},
        {"expected_skill_count": -1},
        {"expected_skill_hash": "a" * 63},
        {"expected_skill_hash": "g" * 64},
        {"expected_context_hash": "A" * 64},
        {"snapshot_poll_seconds": -0.1},
        {"snapshot_timeout_seconds": 0.0},
        {"snapshot_poll_seconds": float("inf")},
        {"prose_interpreter_timeout_seconds": 0.0},
        {"prose_interpreter_timeout_seconds": 301.0},
    ],
)
def test_validate_run_parameters_rejects(overrides: dict[str, object]) -> None:
    with pytest.raises(RunFailure):
        _validate_run_parameters(_params(**overrides))


def test_observed_model_identifiers_collects_nonnull_per_turn() -> None:
    # the summary verifies every turn ran under the pinned model, so the collector must gather each
    # turn's non-null identifier (from a response or an input request) and ignore absent ones.
    turns = [
        {"persisted_response": {"root_model_identifier": "claude-opus-4-8"}},
        {"persisted_response": {"root_model_identifier": None}, "persisted_input_request": None},
        {"persisted_response": None,
         "persisted_input_request": {"root_model_identifier": "claude-sonnet-5"}},
        {"persisted_response": None, "persisted_input_request": None},
    ]
    assert run_episode._observed_model_identifiers(turns) == {
        "claude-opus-4-8",
        "claude-sonnet-5",
    }


# --- input validation ---------------------------------------------------------------------------


def test_validate_inputs_resolves_external_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(run_episode, "_REPOSITORY_ROOT", repo)
    scenario = tmp_path / "scenario.json"
    scenario.write_text("{}", encoding="utf-8")
    source_db = tmp_path / "operon.db"
    source_db.write_bytes(b"db")
    args = argparse.Namespace(scenario=scenario, source_db=source_db)
    resolved_scenario, resolved_db = _validate_inputs(args)
    assert resolved_scenario == scenario.resolve()
    assert resolved_db == source_db.resolve()


def test_validate_inputs_missing_scenario(tmp_path: Path) -> None:
    args = argparse.Namespace(
        scenario=tmp_path / "absent.json", source_db=tmp_path / "operon.db"
    )
    (tmp_path / "operon.db").write_bytes(b"db")
    with pytest.raises(RunFailure):
        _validate_inputs(args)


def test_validate_inputs_missing_source_db(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.json"
    scenario.write_text("{}", encoding="utf-8")
    args = argparse.Namespace(scenario=scenario, source_db=tmp_path / "absent.db")
    with pytest.raises(RunFailure):
        _validate_inputs(args)


def test_validate_inputs_rejects_source_db_inside_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(run_episode, "_REPOSITORY_ROOT", repo)
    scenario = tmp_path / "scenario.json"
    scenario.write_text("{}", encoding="utf-8")
    source_db = repo / "operon.db"
    source_db.write_bytes(b"db")
    args = argparse.Namespace(scenario=scenario, source_db=source_db)
    with pytest.raises(RunFailure):
        _validate_inputs(args)


# --- fixture resolution -------------------------------------------------------------------------


def _write_fixture(path: Path, content: bytes = b"seed-bytes") -> str:
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def test_resolve_fixture_none_without_supplied() -> None:
    assert _resolve_fixture(_scenario(fixture=None), Path("scenario.json"), None) is None


def test_resolve_fixture_none_rejects_supplied(tmp_path: Path) -> None:
    with pytest.raises(RunFailure):
        _resolve_fixture(_scenario(fixture=None), tmp_path / "scenario.json", tmp_path / "x.csv")


def test_resolve_fixture_default_path(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    digest = _write_fixture(fixtures / "seed.csv")
    scenario = _scenario(fixture={"filename": "seed.csv", "sha256": digest})
    resolved = _resolve_fixture(scenario, tmp_path / "scenario.json", None)
    assert resolved == (fixtures / "seed.csv")


def test_resolve_fixture_supplied_path(tmp_path: Path) -> None:
    supplied = tmp_path / "seed.csv"
    digest = _write_fixture(supplied)
    scenario = _scenario(fixture={"filename": "seed.csv", "sha256": digest})
    resolved = _resolve_fixture(scenario, tmp_path / "scenario.json", supplied)
    assert resolved == supplied.resolve()


def test_resolve_fixture_rejects_missing_file(tmp_path: Path) -> None:
    scenario = _scenario(fixture={"filename": "seed.csv", "sha256": "0" * 64})
    with pytest.raises(RunFailure):
        _resolve_fixture(scenario, tmp_path / "scenario.json", tmp_path / "absent.csv")


def test_resolve_fixture_rejects_name_mismatch(tmp_path: Path) -> None:
    supplied = tmp_path / "other.csv"
    digest = _write_fixture(supplied)
    scenario = _scenario(fixture={"filename": "seed.csv", "sha256": digest})
    with pytest.raises(RunFailure):
        _resolve_fixture(scenario, tmp_path / "scenario.json", supplied)


def test_resolve_fixture_rejects_hash_mismatch(tmp_path: Path) -> None:
    supplied = tmp_path / "seed.csv"
    _write_fixture(supplied)
    scenario = _scenario(fixture={"filename": "seed.csv", "sha256": "0" * 64})
    with pytest.raises(RunFailure):
        _resolve_fixture(scenario, tmp_path / "scenario.json", supplied)


# --- trial validation ---------------------------------------------------------------------------


def test_validate_trial_accepts_declared_variant() -> None:
    _validate_trial(_scenario(variants={"bare": "go", "nudged": "go carefully"}), "nudged")


def test_validate_trial_rejects_unknown_variant() -> None:
    with pytest.raises(RunFailure):
        _validate_trial(_scenario(variants={"bare": "go"}), "missing")


# --- snapshot-capture verification --------------------------------------------------------------


def _seed_project_db(path: Path, project_id: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO projects VALUES (?, ?)", (project_id, "demo"))
        conn.commit()
    finally:
        conn.close()


def test_verify_snapshot_capture_accepts_present_project(tmp_path: Path) -> None:
    db = tmp_path / "project.db"
    _seed_project_db(db, "p1")
    _verify_snapshot_capture(db, "p1")


def test_verify_snapshot_capture_rejects_absent_project(tmp_path: Path) -> None:
    db = tmp_path / "project.db"
    _seed_project_db(db, "p1")
    with pytest.raises(RunFailure):
        _verify_snapshot_capture(db, "p2")


# --- small pure helpers -------------------------------------------------------------------------


def test_sorted_set_sha256_is_order_independent() -> None:
    expected = hashlib.sha256(b"a\nb\nc").hexdigest()
    assert sorted_set_sha256(frozenset({"c", "a", "b"})) == expected
    assert sorted_set_sha256(frozenset({"b", "c", "a"})) == expected


def test_episode_id_is_filesystem_safe() -> None:
    episode = _episode_id("PBMC figure/package:v2")
    assert "/" not in episode
    assert episode.startswith("PBMC-figure-package:v2")


@pytest.mark.parametrize(
    ("reason", "classification"),
    [
        ("completed", "completed"),
        ("terminal_observation", "terminal_observation"),
        ("policy_exceeded", "incomplete"),
        ("checkpoint_failed_gate", "incomplete"),
    ],
)
def test_outcome_classification(reason: str, classification: str) -> None:
    assert _outcome_classification(reason) == classification
