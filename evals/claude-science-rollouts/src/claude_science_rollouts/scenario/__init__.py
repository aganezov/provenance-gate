"""Scenario specs + construction checkpoints — the tracked construction label a replicate is
scored against, plus ``compile_scenario`` (scenario → ordered plan). The checkpoint evaluator reuses
the oracle's closure; scenario prompts live in ``scenarios/`` as tracked data.
"""

from __future__ import annotations

from .checkpoints import (
    AssertionResult,
    CheckpointResult,
    all_gates_pass,
    evaluate_checkpoints,
)
from .compiler import Step, compile_scenario
from .spec import (
    ApprovalPolicy,
    ResponseRule,
    Scenario,
    ScenarioError,
    Session,
    Trial,
    Turn,
    load_scenario,
)

__all__ = [
    "evaluate_checkpoints",
    "all_gates_pass",
    "CheckpointResult",
    "AssertionResult",
    "load_scenario",
    "compile_scenario",
    "Scenario",
    "Session",
    "Turn",
    "Trial",
    "ResponseRule",
    "ApprovalPolicy",
    "Step",
    "ScenarioError",
]
