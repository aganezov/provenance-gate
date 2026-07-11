"""Typed scenario spec: sessions, construction turns, a trial, and checkpoints the harness drives.

A scenario is authored data (``scenarios/*.json``); this module loads and validates it into
immutable objects. Sessions carry explicit chat semantics (``new`` vs ``resume``) so a compiler
need not infer chat boundaries; the construction is an ordered turn sequence; the trial is its own
turn in a fresh session; checkpoints are the construction label, pinned to the turn after which they
hold. Validation is fail-closed: an unknown schema version, mode, or assertion kind, an undeclared
session, or a checkpoint pinned to a non-existent turn is rejected before ``Scenario`` is returned.
A fixture must name its ``attach_before`` turn (an un-attached fixture is a silent no-op), and the
approval policy is deny-by-default.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_MODES = frozenset({"gate", "measure"})
_CHAT = frozenset({"new", "resume"})
_APPROVAL_ACTIONS = frozenset({"deny", "allow_for_conversation"})
_ASSERTION_KEYS: dict[str, tuple[str, ...]] = {
    "version_exists": ("artifact", "version"),
    "latest_version": ("artifact", "version"),
    "depends_on": ("consumer", "inputs"),
    "closure_contains": ("node", "artifacts"),
    "checksums_differ": ("artifact", "versions"),
    "checksums_equal": ("artifact", "versions"),
}


class ScenarioError(ValueError):
    """Raised when a scenario file is malformed."""


@dataclass(frozen=True, slots=True)
class Session:
    id: str
    chat: str   # "new" | "resume"


@dataclass(frozen=True, slots=True)
class Turn:
    session: str
    turn_id: str
    prompt: str


@dataclass(frozen=True, slots=True)
class Trial:
    session: str
    turn_id: str
    variants: dict[str, str]


@dataclass(frozen=True, slots=True)
class ResponseRule:
    id: str
    after_turn_id: str
    trigger: str
    reply: str


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """Bounded approval policy the driver applies when a turn stalls on an approval card.

    Python is authoritative: ``deny`` (the default) refuses every card; ``allow_for_conversation``
    grants approvals for the run up to ``max_approvals`` and no further. The browser layer resolves
    the specific card; this only decides whether — and how many times — it may.
    """

    action: str          # "deny" | "allow_for_conversation"
    max_approvals: int   # deny -> 0; allow -> 1..32


_DENY_APPROVALS = ApprovalPolicy(action="deny", max_approvals=0)


@dataclass(frozen=True, slots=True)
class Scenario:
    schema_version: int
    scenario_id: str
    tier: str
    sessions: tuple[Session, ...]
    construction: tuple[Turn, ...]
    trial: Trial
    checkpoints: tuple[dict[str, Any], ...]
    fixture: dict[str, Any] | None = None
    response_rules: tuple[ResponseRule, ...] = ()
    approval_policy: ApprovalPolicy = _DENY_APPROVALS

    def turn(self, turn_id: str) -> Turn:
        for t in self.construction:
            if t.turn_id == turn_id:
                return t
        raise KeyError(turn_id)


def _str(value: Any, ctx: str) -> str:
    if not isinstance(value, str) or not value:
        raise ScenarioError(f"{ctx} must be a non-empty string")
    return value


def _sessions(raw: Any) -> tuple[Session, ...]:
    if not isinstance(raw, list) or not raw:
        raise ScenarioError("sessions must be a non-empty array")
    sessions, seen = [], set()
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            raise ScenarioError(f"sessions[{i}] must be an object")
        sid = _str(s.get("id"), f"sessions[{i}].id")
        if sid in seen:
            raise ScenarioError(f"duplicate session id: {sid!r}")
        if s.get("chat") not in _CHAT:
            raise ScenarioError(f"sessions[{i}].chat must be one of {sorted(_CHAT)}")
        seen.add(sid)
        sessions.append(Session(id=sid, chat=s["chat"]))
    return tuple(sessions)


def _assertion(a: Any, ctx: str) -> None:
    if not isinstance(a, dict) or "kind" not in a:
        raise ScenarioError(f"{ctx}: each assertion must be an object with a 'kind'")
    keys = _ASSERTION_KEYS.get(a["kind"])
    if keys is None:
        raise ScenarioError(f"{ctx}: unknown assertion kind {a['kind']!r}")
    missing = [k for k in keys if k not in a]
    if missing:
        raise ScenarioError(f"{ctx}: assertion {a['kind']!r} missing keys {missing}")


def _fixture(raw: Any, turn_ids: set[str]) -> dict[str, Any] | None:
    """Validate the optional seed fixture. A fixture that never attaches is a silent no-op, so
    ``attach_before`` is required and must name a construction turn (fail-closed)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ScenarioError("fixture must be an object")
    _str(raw.get("filename"), "fixture.filename")
    sha = _str(raw.get("sha256"), "fixture.sha256")
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha.lower()):
        raise ScenarioError("fixture.sha256 must be 64 hex characters")
    attach = _str(raw.get("attach_before"), "fixture.attach_before")
    if attach not in turn_ids:
        raise ScenarioError("fixture.attach_before must name a construction turn")
    return dict(raw)


def _approval_policy(raw: Any) -> ApprovalPolicy:
    """Validate the optional approval policy; absent means deny-by-default (fail-closed)."""
    if raw is None:
        return _DENY_APPROVALS
    if not isinstance(raw, dict):
        raise ScenarioError("approval_policy must be an object")
    extra = set(raw) - {"action", "max_approvals"}
    if extra:
        raise ScenarioError(f"approval_policy has unknown keys {sorted(extra)}")
    action = raw.get("action", "deny")
    if action not in _APPROVAL_ACTIONS:
        raise ScenarioError(f"approval_policy.action must be one of {sorted(_APPROVAL_ACTIONS)}")
    if action == "deny":
        return _DENY_APPROVALS
    maximum = raw.get("max_approvals")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 32:
        raise ScenarioError("approval_policy.max_approvals must be an integer in 1..32")
    return ApprovalPolicy(action=action, max_approvals=maximum)


def _checkpoint(cp: Any, turn_ids: set[str], index: int) -> str:
    ctx = f"checkpoints[{index}]"
    if not isinstance(cp, dict):
        raise ScenarioError(f"{ctx} must be an object")
    cp_id = _str(cp.get("id"), f"{ctx}.id")
    if cp.get("mode", "gate") not in _MODES:
        raise ScenarioError(f"{ctx}.mode must be one of {sorted(_MODES)}")
    if cp.get("after_turn_id") not in turn_ids:
        raise ScenarioError(f"{ctx}.after_turn_id must name a construction turn")
    assertions = cp.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        raise ScenarioError(f"{ctx}.assertions must be a non-empty array")
    for a in assertions:
        _assertion(a, ctx)
    return cp_id


def load_scenario(path: str | Path) -> Scenario:
    """Load and fully validate a tracked scenario JSON into an immutable ``Scenario``."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ScenarioError("scenario must be a JSON object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ScenarioError(f"schema_version must be {SCHEMA_VERSION}")
    scenario_id = _str(raw.get("scenario_id"), "scenario_id")

    sessions = _sessions(raw.get("sessions"))
    session_ids = {s.id for s in sessions}

    turns = raw.get("construction")
    if not isinstance(turns, list) or not turns:
        raise ScenarioError("construction must be a non-empty array")
    construction, turn_ids = [], set()
    for i, t in enumerate(turns):
        if not isinstance(t, dict):
            raise ScenarioError(f"construction[{i}] must be an object")
        session = _str(t.get("session"), f"construction[{i}].session")
        if session not in session_ids:
            raise ScenarioError(f"construction[{i}].session {session!r} is not declared")
        turn_id = _str(t.get("turn_id"), f"construction[{i}].turn_id")
        if turn_id in turn_ids:
            raise ScenarioError(f"duplicate construction turn_id: {turn_id!r}")
        turn_ids.add(turn_id)
        construction.append(Turn(session=session, turn_id=turn_id,
                                 prompt=_str(t.get("prompt"), f"construction[{i}].prompt")))

    tr = raw.get("trial")
    if not isinstance(tr, dict):
        raise ScenarioError("trial must be an object")
    if _str(tr.get("session"), "trial.session") not in session_ids:
        raise ScenarioError("trial.session is not declared")
    trial_turn = _str(tr.get("turn_id"), "trial.turn_id")
    if trial_turn in turn_ids:
        raise ScenarioError("trial.turn_id collides with a construction turn")
    variants = tr.get("variants")
    if not isinstance(variants, dict) or not isinstance(variants.get("bare"), str):
        raise ScenarioError("trial.variants must be an object with at least a 'bare' prompt")
    trial = Trial(session=tr["session"], turn_id=trial_turn, variants=dict(variants))

    checkpoints = raw.get("checkpoints")
    if not isinstance(checkpoints, list) or not checkpoints:
        raise ScenarioError("checkpoints must be a non-empty array")
    cp_ids: set[str] = set()
    for i, cp in enumerate(checkpoints):
        cp_id = _checkpoint(cp, turn_ids, i)
        if cp_id in cp_ids:
            raise ScenarioError(f"duplicate checkpoint id: {cp_id!r}")
        cp_ids.add(cp_id)

    rules = []
    for i, r in enumerate(raw.get("response_rules", []) or []):
        if not isinstance(r, dict):
            raise ScenarioError(f"response_rules[{i}] must be an object")
        after = _str(r.get("after_turn_id"), f"response_rules[{i}].after_turn_id")
        if after not in turn_ids:
            raise ScenarioError(f"response_rules[{i}].after_turn_id must name a construction turn")
        rules.append(ResponseRule(
            id=_str(r.get("id"), f"response_rules[{i}].id"),
            after_turn_id=after,
            trigger=_str(r.get("trigger"), f"response_rules[{i}].trigger"),
            reply=_str(r.get("reply"), f"response_rules[{i}].reply"),
        ))

    return Scenario(
        schema_version=SCHEMA_VERSION,
        scenario_id=scenario_id,
        tier=raw.get("tier", "scientific"),
        sessions=sessions,
        construction=tuple(construction),
        trial=trial,
        checkpoints=tuple(checkpoints),
        fixture=_fixture(raw.get("fixture"), turn_ids),
        response_rules=tuple(rules),
        approval_policy=_approval_policy(raw.get("approval_policy")),
    )
