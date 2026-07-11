"""Tests for compiling a scenario into a deterministic, ordered execution plan."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_science_rollouts.scenario import ScenarioError, compile_scenario, load_scenario

_PBMC = Path(__file__).resolve().parents[1] / "scenarios" / "pbmc_figure_package.json"


def test_compile_pbmc_plan_is_ordered_and_complete():
    plan = compile_scenario(load_scenario(_PBMC), trial="bare")
    ops = [st.op for st in plan]
    assert ops.count("new_chat") == 4          # four fresh-chat sessions
    assert ops.count("attach") == 1
    assert ops.count("gate") == 4              # one step per checkpoint
    submits = [st for st in plan if st.op == "submit"]
    assert len(submits) == 10                  # 9 construction turns + 1 trial
    assert submits[-1].turn_id == "assemble-final"   # the trial is the final turn

    def first(op):
        return next(i for i, st in enumerate(plan) if st.op == op)

    assert first("new_chat") < first("attach") < first("submit")


def test_each_gate_follows_its_turn():
    s = load_scenario(_PBMC)
    plan = compile_scenario(s, trial="bare")
    for cp in s.checkpoints:
        submit_i = next(i for i, st in enumerate(plan)
                        if st.op == "submit" and st.turn_id == cp["after_turn_id"])
        gate_i = next(i for i, st in enumerate(plan)
                      if st.op == "gate" and st.checkpoint_id == cp["id"])
        assert gate_i > submit_i


def test_compile_rejects_unknown_trial_variant():
    with pytest.raises(ScenarioError, match="trial variant"):
        compile_scenario(load_scenario(_PBMC), trial="nope")


def test_returning_to_an_earlier_session_re_opens_its_chat(tmp_path):
    """Regression: an A -> B -> A construction must re-focus A before its second turn, or the turn
    would submit into B's active chat. The compiler tracks the active session, not just which were
    opened, so revisiting a session emits an ``open_chat`` for it."""
    scenario = {
        "schema_version": 1,
        "scenario_id": "aba",
        "sessions": [{"id": "A", "chat": "new"}, {"id": "B", "chat": "new"}],
        "construction": [
            {"session": "A", "turn_id": "a1", "prompt": "p"},
            {"session": "B", "turn_id": "b1", "prompt": "p"},
            {"session": "A", "turn_id": "a2", "prompt": "p"},
        ],
        "trial": {"session": "A", "turn_id": "t", "variants": {"bare": "b"}},
        "checkpoints": [
            {"id": "c", "mode": "gate", "after_turn_id": "a1",
             "assertions": [{"kind": "version_exists", "artifact": "x", "version": 1}]}
        ],
    }
    p = tmp_path / "aba.json"
    p.write_text(json.dumps(scenario))
    plan = compile_scenario(load_scenario(p), trial="bare")

    def session_active_at(submit_turn):
        # the session focused by the most recent new_chat/open_chat before this submit
        active = None
        for st in plan:
            if st.op in ("new_chat", "open_chat"):
                active = st.session
            if st.op == "submit" and st.turn_id == submit_turn:
                return active
        raise AssertionError(f"no submit for {submit_turn!r}")

    assert session_active_at("a1") == "A"
    assert session_active_at("b1") == "B"
    assert session_active_at("a2") == "A"   # the bug submitted a2 while B was still active
    assert session_active_at("t") == "A"    # trial returns to A too
    # A is created once (new_chat), then re-opened on return (open_chat) — never new_chat twice
    assert [st.op for st in plan if st.session == "A"].count("new_chat") == 1
    assert any(st.op == "open_chat" and st.session == "A" for st in plan)
