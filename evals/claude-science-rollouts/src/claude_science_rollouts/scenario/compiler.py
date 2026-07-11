"""Compile a validated Scenario into a deterministic, ordered execution plan.

The plan is what the browser bridge consumes: one step per browser operation, with chat lifecycle
resolved (new_chat / open_chat), the fixture attached once before its turn, each construction prompt
submitted in order, a construction gate placed after the turn it holds, and the trial submitted as
its own turn. This is Python-owned scenario logic; turning a step into a versioned browser request
(``materialize_browser_request``) is the bridge's job, not this module's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .spec import Scenario, ScenarioError


@dataclass(frozen=True, slots=True)
class Step:
    op: str                       # "new_chat" | "open_chat" | "attach" | "submit" | "gate"
    session: str | None = None
    turn_id: str | None = None
    prompt: str | None = None
    checkpoint_id: str | None = None
    fixture: dict[str, Any] | None = None


def compile_scenario(scenario: Scenario, *, trial: str = "bare") -> tuple[Step, ...]:
    """Ordered plan for one trial variant. Deterministic; no browser, no I/O."""
    if trial not in scenario.trial.variants:
        raise ScenarioError(f"unknown trial variant {trial!r}")
    chat_of = {s.id: s.chat for s in scenario.sessions}
    gates_after: dict[str, list[str]] = {}
    for cp in scenario.checkpoints:
        gates_after.setdefault(cp["after_turn_id"], []).append(cp["id"])

    steps: list[Step] = []
    opened: set[str] = set()
    active: str | None = None

    def focus(session_id: str) -> None:
        """Bring ``session_id`` to the front. A session is created once (new/open per its chat
        semantics); revisiting an already-created session re-opens its chat. Emits nothing when the
        session is already active — so contiguous turns don't churn, but a return after a detour
        (A -> B -> A) re-focuses A instead of silently submitting into B."""
        nonlocal active
        if active == session_id:
            return
        if session_id not in opened:
            op = "new_chat" if chat_of[session_id] == "new" else "open_chat"
            opened.add(session_id)
        else:
            op = "open_chat"
        steps.append(Step(op=op, session=session_id))
        active = session_id

    attach_before = (scenario.fixture or {}).get("attach_before")
    for t in scenario.construction:
        focus(t.session)
        if attach_before == t.turn_id:
            steps.append(Step(op="attach", session=t.session, fixture=scenario.fixture))
        steps.append(Step(op="submit", session=t.session, turn_id=t.turn_id, prompt=t.prompt))
        for cp_id in gates_after.get(t.turn_id, []):
            steps.append(Step(op="gate", checkpoint_id=cp_id))

    tr = scenario.trial
    focus(tr.session)
    steps.append(
        Step(op="submit", session=tr.session, turn_id=tr.turn_id, prompt=tr.variants[trial])
    )
    return tuple(steps)
