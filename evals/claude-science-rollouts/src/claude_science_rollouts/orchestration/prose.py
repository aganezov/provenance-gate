"""Unattended response policy: reduce a classifier's reading of terminal prose to one action.

When a turn ends in prose rather than an execution approval, the harness still has to decide what to
do next without a human in the loop. It hands the agent's terminal text to an out-of-process
classifier, which reports a structured reading: how the prose relates to a lineage issue, which
canonical-reply rule (if any) it would trigger, and a verbatim excerpt it based that on. This module
turns that reading into exactly one action and fails closed whenever the evidence does not line up.

Two guardrails keep an unattended reply honest before any classification is even considered. The
excerpt must appear exactly once in the response, so a later reviewer can point at the precise span
the decision hinged on; zero or several matches means we cannot pin the evidence. And any rule the
classifier names must be one the harness actually offered for this turn. Only a correction that both
names an offered rule earns the single side-effecting action; every other shape either continues
untouched or stops for manual adjudication.

The classifier itself stays behind a subprocess boundary. ``SubprocessProseInterpreter`` writes both
halves of the exchange to an evidence directory so the decision is reconstructable after the fact,
and bounds the process's runtime and output so a misbehaving classifier cannot wedge or flood the
run.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, get_args

from claude_science_rollouts.capture.evidence import canonical_json

# how the classifier reads the prose against the turn's lineage concern. only a correction that is
# actually offered can produce a reply; the rest are recorded and either waved through or halted.
Classification = Literal[
    "scientific_caveat_only",
    "lineage_issue_identified",
    "lineage_correction_offered",
    "no_relevant_issue",
    "ambiguous",
]
DecisionAction = Literal["continue", "submit_canonical_reply", "stop"]

_CLASSIFICATIONS: frozenset[str] = frozenset(get_args(Classification))

# an assistant answer above this size is treated as evidence we cannot vouch for; the excerpt and
# the whole subprocess payload get their own, tighter and looser, ceilings.
_MAX_RESPONSE_BYTES = 32_768
_MAX_EXCERPT_BYTES = 2_048
_MAX_RESULT_BYTES = 65_536
_MAX_RULE_ID_BYTES = 128
_MAX_RULES = 16


class ProseInterpretationError(RuntimeError):
    """An interpreter request, result, or policy selection failed closed."""


def _bounded_text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be non-empty text")
    if len(value.encode()) > limit:
        raise ValueError(f"{name} exceeds {limit} bytes")
    return value


def _rule_id(value: Any, name: str) -> str:
    # rule ids travel through logs and back into the harness as identifiers, so they stay short and
    # whitespace-free rather than free text.
    text = _bounded_text(value, name, _MAX_RULE_ID_BYTES)
    if any(character.isspace() for character in text):
        raise ValueError(f"{name} cannot contain whitespace")
    return text


@dataclass(frozen=True, slots=True)
class ProseInterpretationRequest:
    """The terminal prose plus the rule ids the harness is willing to let a reply trigger."""

    response_text: str
    eligible_rule_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _bounded_text(self.response_text, "response_text", _MAX_RESPONSE_BYTES)
        if not isinstance(self.eligible_rule_ids, tuple):
            raise ValueError("eligible_rule_ids must be a tuple")
        if len(self.eligible_rule_ids) > _MAX_RULES:
            raise ValueError("too many eligible rule IDs")
        normalized = tuple(_rule_id(value, "eligible_rule_id") for value in self.eligible_rule_ids)
        if len(set(normalized)) != len(normalized):
            raise ValueError("eligible rule IDs must be unique")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "response_text": self.response_text,
            "eligible_rule_ids": list(self.eligible_rule_ids),
        }


@dataclass(frozen=True, slots=True)
class ProseInterpretationResult:
    """The classifier's structured reading of one response."""

    classification: Classification
    eligible_rule_id: str | None
    evidence_excerpt: str

    def __post_init__(self) -> None:
        if self.classification not in _CLASSIFICATIONS:
            raise ValueError("unsupported prose classification")
        if self.eligible_rule_id is not None:
            _rule_id(self.eligible_rule_id, "eligible_rule_id")
        _bounded_text(self.evidence_excerpt, "evidence_excerpt", _MAX_EXCERPT_BYTES)

    @classmethod
    def from_dict(cls, value: Any) -> ProseInterpretationResult:
        # the wire form carries a schema_version and nothing else; an unexpected key set means we
        # are reading a shape we did not author, so reject rather than guess.
        if not isinstance(value, dict):
            raise ValueError("interpreter result must be an object")
        expected = {"schema_version", "classification", "eligible_rule_id", "evidence_excerpt"}
        if set(value) != expected or value.get("schema_version") != 1:
            raise ValueError("interpreter result fields are invalid")
        return cls(
            value["classification"],
            value["eligible_rule_id"],
            value["evidence_excerpt"],
        )

    def as_dict(self) -> dict[str, Any]:
        # the reading is spread into a wider decision record by the caller, so it stays flat and
        # omits the schema_version that only the wire form needs.
        return {
            "classification": self.classification,
            "eligible_rule_id": self.eligible_rule_id,
            "evidence_excerpt": self.evidence_excerpt,
        }


@dataclass(frozen=True, slots=True)
class ProseDecision:
    """The single action the policy selected, and the rule a reply would carry."""

    action: DecisionAction
    rule_id: str | None


class ProseInterpreter(Protocol):
    def interpret(self, request: ProseInterpretationRequest) -> ProseInterpretationResult: ...


def decide_interpretation(
    request: ProseInterpretationRequest,
    result: ProseInterpretationResult,
) -> ProseDecision:
    """Validate the evidence and reduce one reading to a single bounded action."""
    # the excerpt has to name an unambiguous span of the response we can point back at; anything but
    # one occurrence leaves the decision unauditable.
    if request.response_text.count(result.evidence_excerpt) != 1:
        return ProseDecision("stop", None)
    # a reply may only trigger a rule the harness put on the table for this turn.
    if (
        result.eligible_rule_id is not None
        and result.eligible_rule_id not in request.eligible_rule_ids
    ):
        return ProseDecision("stop", None)
    # the one shape that earns a reply is an offered correction that names its rule.
    if result.classification == "lineage_correction_offered":
        if result.eligible_rule_id is None:
            return ProseDecision("stop", None)
        return ProseDecision("submit_canonical_reply", result.eligible_rule_id)
    # any other classification carrying a rule is a contradiction, not a reply we act on.
    if result.eligible_rule_id is not None:
        return ProseDecision("stop", None)
    return ProseDecision("continue", None)


class SubprocessProseInterpreter:
    """Run the classifier as one external process per request, recording both halves as evidence."""

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        evidence_dir: Path,
        timeout_seconds: float = 120.0,
    ) -> None:
        if not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("interpreter command must be a non-empty argument tuple")
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError("interpreter timeout must be in (0, 300] seconds")
        self.command = command
        self.evidence_dir = evidence_dir
        self.timeout_seconds = timeout_seconds
        self._sequence = 0

    def interpret(self, request: ProseInterpretationRequest) -> ProseInterpretationResult:
        self._sequence += 1
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"interpretation-{self._sequence:04d}"
        # canonical, key-sorted encoding so the request bytes we hand the process match the bytes we
        # keep on disk for the record.
        request_bytes = canonical_json(request.as_dict()).encode()
        (self.evidence_dir / f"{prefix}-request.json").write_bytes(request_bytes)

        try:
            completed = subprocess.run(
                self.command,
                input=request_bytes,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # a timed-out run still leaves whatever it emitted before we killed it.
            self._persist_streams(prefix, exc.stdout or b"", exc.stderr or b"")
            raise ProseInterpretationError("interpreter process did not complete") from exc
        except OSError as exc:
            raise ProseInterpretationError("interpreter process did not complete") from exc

        # persist both streams before any judgement, so an oversize, nonzero-exit, or unparseable
        # run still leaves a trail on disk.
        self._persist_streams(prefix, completed.stdout, completed.stderr)
        if len(completed.stdout) > _MAX_RESULT_BYTES or len(completed.stderr) > _MAX_RESULT_BYTES:
            raise ProseInterpretationError("interpreter output exceeds the bound")
        if completed.returncode != 0:
            raise ProseInterpretationError("interpreter process exited nonzero")

        try:
            return ProseInterpretationResult.from_dict(json.loads(completed.stdout))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProseInterpretationError("interpreter returned invalid JSON") from exc

    def _persist_streams(self, prefix: str, stdout: bytes, stderr: bytes) -> None:
        (self.evidence_dir / f"{prefix}-stdout.json").write_bytes(stdout)
        (self.evidence_dir / f"{prefix}-stderr.log").write_bytes(stderr)
