"""Offline coverage for the unattended prose policy and its subprocess classifier adapter.

The policy function is pure, so it is exercised directly. The subprocess interpreter is driven
against a tiny local stub script written into a temp dir and invoked as ``(sys.executable, stub)``:
it reads stdin and echoes a canned line, so nothing here touches a network, a real classifier, or a
browser.
The one thing this cannot fully mimic is a classifier that is slow *and* well-behaved; the timeout
path is covered with a stub that simply sleeps until it is killed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from claude_science_rollouts.capture.evidence import canonical_json
from claude_science_rollouts.orchestration.prose import (
    ProseDecision,
    ProseInterpretationError,
    ProseInterpretationRequest,
    ProseInterpretationResult,
    SubprocessProseInterpreter,
    decide_interpretation,
)

_RESPONSE = "The join reuses cohort v1 while the counts came from cohort v3."
_EXCERPT = "cohort v1 while the counts came from cohort v3"
_RULE = "canonical.cohort-version"


def _request(*, response: str = _RESPONSE, rules: tuple[str, ...] = (_RULE,)):
    return ProseInterpretationRequest(response, rules)


def _result(
    *,
    classification: str = "lineage_correction_offered",
    rule_id: str | None = _RULE,
    excerpt: str = _EXCERPT,
):
    return ProseInterpretationResult(classification, rule_id, excerpt)


# --- decide_interpretation: every branch of the policy ---


def test_offered_correction_with_offered_rule_submits_reply() -> None:
    decision = decide_interpretation(_request(), _result())
    assert decision == ProseDecision("submit_canonical_reply", _RULE)


def test_clean_reading_continues() -> None:
    decision = decide_interpretation(
        _request(), _result(classification="no_relevant_issue", rule_id=None)
    )
    assert decision == ProseDecision("continue", None)


def test_offered_correction_without_a_rule_stops() -> None:
    decision = decide_interpretation(
        _request(), _result(classification="lineage_correction_offered", rule_id=None)
    )
    assert decision == ProseDecision("stop", None)


def test_non_correction_carrying_a_rule_stops() -> None:
    # a rule attached to anything other than an offered correction is a contradiction we refuse to
    # act on, even though the rule itself is eligible.
    decision = decide_interpretation(
        _request(), _result(classification="lineage_issue_identified", rule_id=_RULE)
    )
    assert decision == ProseDecision("stop", None)


def test_rule_outside_the_offered_set_stops() -> None:
    decision = decide_interpretation(
        _request(rules=("canonical.other",)),
        _result(rule_id=_RULE),
    )
    assert decision == ProseDecision("stop", None)


def test_excerpt_that_is_absent_stops() -> None:
    decision = decide_interpretation(_request(), _result(excerpt="not in the response"))
    assert decision == ProseDecision("stop", None)


def test_excerpt_that_repeats_stops() -> None:
    # the excerpt has to pin one span; two occurrences make the cited evidence ambiguous.
    decision = decide_interpretation(
        _request(response="ready ready", rules=()),
        _result(classification="no_relevant_issue", rule_id=None, excerpt="ready"),
    )
    assert decision == ProseDecision("stop", None)


# --- request / result validation and encoding ---


def test_request_rejects_empty_and_oversize_text() -> None:
    with pytest.raises(ValueError):
        ProseInterpretationRequest("", (_RULE,))
    with pytest.raises(ValueError):
        ProseInterpretationRequest("x" * 32_769, (_RULE,))


def test_request_rejects_bad_rule_collections() -> None:
    with pytest.raises(ValueError):
        ProseInterpretationRequest(_RESPONSE, [_RULE])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ProseInterpretationRequest(_RESPONSE, tuple(f"rule-{i}" for i in range(17)))
    with pytest.raises(ValueError):
        ProseInterpretationRequest(_RESPONSE, (_RULE, _RULE))
    with pytest.raises(ValueError):
        ProseInterpretationRequest(_RESPONSE, ("has space",))


def test_result_rejects_invalid_fields() -> None:
    with pytest.raises(ValueError):
        ProseInterpretationResult("nonsense", None, _EXCERPT)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ProseInterpretationResult("no_relevant_issue", "bad id", _EXCERPT)
    with pytest.raises(ValueError):
        ProseInterpretationResult("no_relevant_issue", None, "x" * 2_049)


def test_request_encodes_schema_and_rules() -> None:
    assert _request().as_dict() == {
        "schema_version": 1,
        "response_text": _RESPONSE,
        "eligible_rule_ids": [_RULE],
    }


def test_result_round_trips_through_the_wire_form() -> None:
    result = _result()
    # as_dict is the flat record form; the wire form the classifier emits adds the schema tag, and
    # from_dict must recover the same result from it.
    wire = {"schema_version": 1, **result.as_dict()}
    assert ProseInterpretationResult.from_dict(wire) == result


def test_result_from_dict_rejects_unexpected_shapes() -> None:
    valid = {"schema_version": 1, **_result().as_dict()}
    with pytest.raises(ValueError):
        ProseInterpretationResult.from_dict([1, 2, 3])
    with pytest.raises(ValueError):
        ProseInterpretationResult.from_dict(
            {k: v for k, v in valid.items() if k != "schema_version"}
        )
    with pytest.raises(ValueError):
        ProseInterpretationResult.from_dict({**valid, "schema_version": 2})
    with pytest.raises(ValueError):
        ProseInterpretationResult.from_dict({**valid, "surprise": True})


# --- SubprocessProseInterpreter: offline, against a local stub ---


def _write_stub(tmp_path: Path, name: str, body: str) -> Path:
    stub = tmp_path / name
    stub.write_text("import sys\nsys.stdin.buffer.read()\n" + body)
    return stub


def _interpreter(
    command: tuple[str, ...], evidence_dir: Path, **kwargs
) -> SubprocessProseInterpreter:
    return SubprocessProseInterpreter(command, evidence_dir=evidence_dir, **kwargs)


def test_interpret_records_evidence_and_parses_result(tmp_path: Path) -> None:
    reading = {"schema_version": 1, **_result().as_dict()}
    stub = _write_stub(
        tmp_path,
        "ok.py",
        f"import json\nsys.stdout.write(json.dumps({reading!r}))\n",
    )
    evidence_dir = tmp_path / "evidence"
    interpreter = _interpreter((sys.executable, str(stub)), evidence_dir)

    request = _request()
    result = interpreter.interpret(request)
    assert result == _result()

    # the request is persisted in the same canonical bytes the process received; the parsed reply is
    # persisted verbatim; a stderr trail exists even when empty.
    request_bytes = canonical_json(request.as_dict()).encode()
    assert (evidence_dir / "interpretation-0001-request.json").read_bytes() == request_bytes
    assert json.loads((evidence_dir / "interpretation-0001-stdout.json").read_text()) == reading
    assert (evidence_dir / "interpretation-0001-stderr.log").exists()


def test_interpret_numbers_each_call(tmp_path: Path) -> None:
    reading = {"schema_version": 1, **_result().as_dict()}
    stub = _write_stub(
        tmp_path, "ok.py", f"import json\nsys.stdout.write(json.dumps({reading!r}))\n"
    )
    evidence_dir = tmp_path / "evidence"
    interpreter = _interpreter((sys.executable, str(stub)), evidence_dir)
    interpreter.interpret(_request())
    interpreter.interpret(_request())
    assert (evidence_dir / "interpretation-0002-request.json").exists()


def test_construction_rejects_bad_command_and_timeout(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SubprocessProseInterpreter((), evidence_dir=tmp_path)
    with pytest.raises(ValueError):
        SubprocessProseInterpreter(("",), evidence_dir=tmp_path)
    with pytest.raises(ValueError):
        SubprocessProseInterpreter(("cmd",), evidence_dir=tmp_path, timeout_seconds=0)
    with pytest.raises(ValueError):
        SubprocessProseInterpreter(("cmd",), evidence_dir=tmp_path, timeout_seconds=301)


def test_malformed_stub_output_fails_closed(tmp_path: Path) -> None:
    stub = _write_stub(tmp_path, "garbage.py", "sys.stdout.write('not json at all')\n")
    interpreter = _interpreter((sys.executable, str(stub)), tmp_path / "evidence")
    with pytest.raises(ProseInterpretationError):
        interpreter.interpret(_request())
    # even a rejected run leaves the raw output behind for inspection.
    assert (tmp_path / "evidence" / "interpretation-0001-stdout.json").exists()


def test_wellformed_json_with_wrong_fields_fails_closed(tmp_path: Path) -> None:
    stub = _write_stub(
        tmp_path, "wrong.py", "import json\nsys.stdout.write(json.dumps({'a': 1}))\n"
    )
    interpreter = _interpreter((sys.executable, str(stub)), tmp_path / "evidence")
    with pytest.raises(ProseInterpretationError):
        interpreter.interpret(_request())


def test_nonzero_exit_fails_closed(tmp_path: Path) -> None:
    reading = {"schema_version": 1, **_result().as_dict()}
    stub = _write_stub(
        tmp_path,
        "boom.py",
        f"import json\nsys.stdout.write(json.dumps({reading!r}))\nsys.exit(3)\n",
    )
    interpreter = _interpreter((sys.executable, str(stub)), tmp_path / "evidence")
    with pytest.raises(ProseInterpretationError):
        interpreter.interpret(_request())


def test_oversize_output_fails_closed(tmp_path: Path) -> None:
    # a flood of stdout past the payload ceiling is rejected before any parse is attempted.
    stub = _write_stub(tmp_path, "flood.py", "sys.stdout.write('x' * 70000)\n")
    interpreter = _interpreter((sys.executable, str(stub)), tmp_path / "evidence")
    with pytest.raises(ProseInterpretationError):
        interpreter.interpret(_request())


def test_timeout_fails_closed(tmp_path: Path) -> None:
    stub = _write_stub(tmp_path, "slow.py", "import time\ntime.sleep(30)\n")
    interpreter = _interpreter(
        (sys.executable, str(stub)), tmp_path / "evidence", timeout_seconds=0.5
    )
    with pytest.raises(ProseInterpretationError):
        interpreter.interpret(_request())
