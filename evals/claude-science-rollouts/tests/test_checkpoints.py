"""Tests for the construction checkpoint evaluator against a synthetic PBMC construction.

The tracked gates (``scenarios/pbmc_figure_package.json``) verify the *pre-trial* construction is a
version-inconsistent panel set. They must pass on a faithful construction, reject the clean control,
and reject a mis-wired panel; ``all_gates_pass`` must be fail-closed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_science_rollouts.scenario import all_gates_pass, evaluate_checkpoints
from operon_fixture import Operon, pbmc

_CHECKPOINTS = json.loads(
    (Path(__file__).resolve().parents[1] / "scenarios" / "pbmc_figure_package.json").read_text()
)["checkpoints"]


def _gates(op):
    return evaluate_checkpoints(op.conn, op.pid, _CHECKPOINTS)


def test_pbmc_construction_passes_all_gates():
    op = Operon()
    pbmc(op, conflict=True)
    results = _gates(op)
    assert all_gates_pass(results) is True
    assert all(r.passed for r in results)


def test_clean_control_fails_the_conflict_gates():
    op = Operon()
    pbmc(op, conflict=False)   # IFN recomputed under the SAME QC — no cells.qc.csv v2
    results = _gates(op)
    assert all_gates_pass(results) is False
    assert {"strict-qc-reversion", "strict-ifn-panel"} <= {r.id for r in results if not r.passed}


def test_miswired_ifn_panel_is_rejected():
    """Regression (review blocker 2): panel_ifn v2 must derive from ifn_signature v2."""
    op = Operon()
    pbmc(op, conflict=True)
    cur = op.conn.execute(
        "UPDATE artifact_dependencies SET depends_on_version_id='v_a_cells.qc.csv_2' "
        "WHERE artifact_version_id='v_a_panel_ifn.csv_2'"
    )
    # Guard the fixture's ID scheme: if the edge below stops matching, the mis-wiring never happens
    # and this regression would pass vacuously. Fail loudly instead.
    assert cur.rowcount == 1
    results = _gates(op)
    assert all_gates_pass(results) is False
    assert "strict-ifn-panel" in {r.id for r in results if not r.passed}


def test_all_gates_pass_is_fail_closed():
    """Regression (review blocker 3): empty / gate-less results are NOT scoreable."""
    assert all_gates_pass([]) is False
    op = Operon()
    pbmc(op)
    measure_only = [
        {"id": "m", "mode": "measure",
         "assertions": [{"kind": "version_exists", "artifact": "cells.csv", "version": 1}]}
    ]
    assert all_gates_pass(evaluate_checkpoints(op.conn, op.pid, measure_only)) is False


def test_unknown_mode_and_kind_raise():
    op = Operon()
    with pytest.raises(ValueError, match="unknown mode"):
        evaluate_checkpoints(op.conn, op.pid, [{"id": "x", "mode": "advisory", "assertions": []}])
    with pytest.raises(ValueError, match="unknown checkpoint assertion kind"):
        evaluate_checkpoints(op.conn, op.pid, [{"id": "x", "assertions": [{"kind": "nope"}]}])


def test_checksum_assertions():
    op = Operon()
    op.artifact("x.csv")
    op.version("a_x.csv", 1, checksum="a" * 64)
    op.version("a_x.csv", 2, checksum="b" * 64)   # distinct content
    cps = [
        {"id": "differ", "mode": "measure",
         "assertions": [{"kind": "checksums_differ", "artifact": "x.csv", "versions": [1, 2]}]},
        {"id": "equal", "mode": "measure",
         "assertions": [{"kind": "checksums_equal", "artifact": "x.csv", "versions": [1, 2]}]},
    ]
    passed = {r.id: r.passed for r in evaluate_checkpoints(op.conn, op.pid, cps)}
    assert passed == {"differ": True, "equal": False}
