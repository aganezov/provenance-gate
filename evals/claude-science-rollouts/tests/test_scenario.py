"""Tests for loading + validating the tracked scenario specs and gating a construction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_science_rollouts.scenario import (
    ScenarioError,
    all_gates_pass,
    evaluate_checkpoints,
    load_scenario,
)
from operon_fixture import Operon, pbmc

_SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"


def test_pbmc_scenario_loads_and_validates():
    s = load_scenario(_SCENARIOS / "pbmc_figure_package.json")
    assert s.schema_version == 1
    assert s.scenario_id == "pbmc-figure-package"
    assert [x.id for x in s.sessions] == [
        "initial-build", "style-update", "scientific-update", "trial",
    ]
    assert len(s.construction) == 9
    assert s.turn("strict-qc").session == "scientific-update"
    assert s.trial.session == "trial"
    assert set(s.trial.variants) >= {"bare", "nudged"}
    assert len(s.checkpoints) == 4
    assert len(s.fixture["sha256"]) == 64
    assert s.fixture["attach_before"] == "save-inputs"
    assert s.response_rules[0].id == "decline-sibling-regen"
    assert s.approval_policy.action == "allow_for_conversation"
    assert s.approval_policy.max_approvals == 8


def test_loaded_checkpoints_gate_the_construction():
    """The tracked gates pass on a faithful construction and fail on the clean control."""
    s = load_scenario(_SCENARIOS / "pbmc_figure_package.json")
    planted = Operon()
    pbmc(planted, conflict=True)
    assert all_gates_pass(evaluate_checkpoints(planted.conn, planted.pid, list(s.checkpoints)))
    control = Operon()
    pbmc(control, conflict=False)
    assert not all_gates_pass(evaluate_checkpoints(control.conn, control.pid, list(s.checkpoints)))


def _minimal(**overrides):
    base = {
        "schema_version": 1,
        "scenario_id": "x",
        "sessions": [{"id": "s", "chat": "new"}],
        "construction": [{"session": "s", "turn_id": "t1", "prompt": "p"}],
        "trial": {"session": "s", "turn_id": "trial", "variants": {"bare": "b"}},
        "checkpoints": [
            {"id": "c", "mode": "gate", "after_turn_id": "t1",
             "assertions": [{"kind": "version_exists", "artifact": "a", "version": 1}]}
        ],
    }
    base.update(overrides)
    return base


def _load(tmp_path, obj):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(obj))
    return load_scenario(p)


def test_minimal_scenario_is_valid(tmp_path):
    assert _load(tmp_path, _minimal()).scenario_id == "x"


def test_bad_schema_version_raises(tmp_path):
    with pytest.raises(ScenarioError, match="schema_version"):
        _load(tmp_path, _minimal(schema_version=2))


def test_undeclared_session_raises(tmp_path):
    bad = _minimal(construction=[{"session": "ghost", "turn_id": "t1", "prompt": "p"}])
    with pytest.raises(ScenarioError, match="not declared"):
        _load(tmp_path, bad)


def test_checkpoint_on_unknown_turn_raises(tmp_path):
    cp = {"id": "c", "mode": "gate", "after_turn_id": "nope",
          "assertions": [{"kind": "version_exists", "artifact": "a", "version": 1}]}
    with pytest.raises(ScenarioError, match="after_turn_id"):
        _load(tmp_path, _minimal(checkpoints=[cp]))


def test_unknown_assertion_kind_rejected(tmp_path):
    cp = {"id": "c", "mode": "gate", "after_turn_id": "t1", "assertions": [{"kind": "not_a_kind"}]}
    with pytest.raises(ScenarioError, match="unknown assertion kind"):
        _load(tmp_path, _minimal(checkpoints=[cp]))


def test_checkpoint_id_with_separators_or_dots_rejected(tmp_path):
    # the id becomes a snapshot path component, so a separator or dot-segment (e.g. '../final') that
    # could escape the checkpoints directory must be rejected at load, not resolved on disk.
    for bad_id in ("../final", "a/b", "..", ".hidden"):
        cp = {"id": bad_id, "mode": "gate", "after_turn_id": "t1",
              "assertions": [{"kind": "version_exists", "artifact": "a", "version": 1}]}
        with pytest.raises(ScenarioError, match="filesystem-safe slug"):
            _load(tmp_path, _minimal(checkpoints=[cp]))


_GOOD_SHA = "0" * 64


def test_fixture_validates_and_loads(tmp_path):
    fx = {"filename": "seed.csv", "sha256": _GOOD_SHA, "attach_before": "t1"}
    s = _load(tmp_path, _minimal(fixture=fx))
    assert s.fixture["filename"] == "seed.csv"


def test_fixture_bad_sha256_rejected(tmp_path):
    fx = {"filename": "seed.csv", "sha256": "deadbeef", "attach_before": "t1"}
    with pytest.raises(ScenarioError, match="sha256"):
        _load(tmp_path, _minimal(fixture=fx))


def test_fixture_attach_before_must_name_a_turn(tmp_path):
    fx = {"filename": "seed.csv", "sha256": _GOOD_SHA, "attach_before": "ghost"}
    with pytest.raises(ScenarioError, match="attach_before"):
        _load(tmp_path, _minimal(fixture=fx))


def test_fixture_without_attach_before_rejected(tmp_path):
    # an un-attached fixture is a silent no-op — reject it at load, not at compile
    fx = {"filename": "seed.csv", "sha256": _GOOD_SHA}
    with pytest.raises(ScenarioError, match="attach_before"):
        _load(tmp_path, _minimal(fixture=fx))


def test_approval_policy_defaults_to_deny(tmp_path):
    s = _load(tmp_path, _minimal())
    assert s.approval_policy.action == "deny"
    assert s.approval_policy.max_approvals == 0


def test_approval_policy_allow_for_conversation_loads(tmp_path):
    pol = {"action": "allow_for_conversation", "max_approvals": 4}
    s = _load(tmp_path, _minimal(approval_policy=pol))
    assert s.approval_policy.max_approvals == 4


def test_approval_policy_unknown_action_rejected(tmp_path):
    with pytest.raises(ScenarioError, match="approval_policy.action"):
        _load(tmp_path, _minimal(approval_policy={"action": "allow_all", "max_approvals": 4}))


def test_approval_policy_max_out_of_range_rejected(tmp_path):
    pol = {"action": "allow_for_conversation", "max_approvals": 99}
    with pytest.raises(ScenarioError, match="max_approvals"):
        _load(tmp_path, _minimal(approval_policy=pol))


def test_approval_policy_unknown_key_rejected(tmp_path):
    pol = {"action": "allow_for_conversation", "max_approvals": 4, "titles": ["x"]}
    with pytest.raises(ScenarioError, match="unknown keys"):
        _load(tmp_path, _minimal(approval_policy=pol))


def _with_assertion(assertion):
    """A minimal scenario whose sole gate carries ``assertion`` — for load-time shape checks."""
    return _minimal(checkpoints=[
        {"id": "c", "mode": "gate", "after_turn_id": "t1", "assertions": [assertion]}
    ])


def test_checksums_differ_requires_two_versions(tmp_path):
    # regression (review high): a single-element list makes checksums_differ trivially true
    bad = {"kind": "checksums_differ", "artifact": "a", "versions": [1]}
    with pytest.raises(ScenarioError, match="at least 2 version numbers"):
        _load(tmp_path, _with_assertion(bad))


def test_version_must_be_an_integer(tmp_path):
    bad = {"kind": "version_exists", "artifact": "a", "version": "1"}
    with pytest.raises(ScenarioError, match="must be an integer"):
        _load(tmp_path, _with_assertion(bad))


def test_version_bool_is_not_an_integer(tmp_path):
    bad = {"kind": "version_exists", "artifact": "a", "version": True}
    with pytest.raises(ScenarioError, match="must be an integer"):
        _load(tmp_path, _with_assertion(bad))


def test_depends_on_empty_inputs_rejected(tmp_path):
    bad = {"kind": "depends_on", "consumer": {"artifact": "a", "version": 1}, "inputs": []}
    with pytest.raises(ScenarioError, match="'inputs' must be a non-empty array"):
        _load(tmp_path, _with_assertion(bad))


def test_depends_on_malformed_consumer_rejected(tmp_path):
    bad = {"kind": "depends_on", "consumer": "a",
           "inputs": [{"artifact": "b", "version": 1}]}
    with pytest.raises(ScenarioError, match="consumer must be an object"):
        _load(tmp_path, _with_assertion(bad))


def test_closure_contains_malformed_artifacts_rejected(tmp_path):
    bad = {"kind": "closure_contains", "node": {"artifact": "a", "version": 1}, "artifacts": []}
    with pytest.raises(ScenarioError, match="'artifacts' must be a non-empty object"):
        _load(tmp_path, _with_assertion(bad))


def test_valid_structured_assertions_load(tmp_path):
    ok = {"kind": "depends_on", "consumer": {"artifact": "a", "version": 2},
          "inputs": [{"artifact": "b", "version": 1}, {"artifact": "c", "version": 1}]}
    assert _load(tmp_path, _with_assertion(ok)).scenario_id == "x"


def test_duplicate_response_rule_id_rejected(tmp_path):
    rules = [
        {"id": "r", "after_turn_id": "t1", "trigger": "x", "reply": "y"},
        {"id": "r", "after_turn_id": "t1", "trigger": "z", "reply": "w"},
    ]
    with pytest.raises(ScenarioError, match="duplicate response_rule id"):
        _load(tmp_path, _minimal(response_rules=rules))
