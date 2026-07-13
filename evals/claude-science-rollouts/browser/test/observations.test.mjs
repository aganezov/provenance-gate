import assert from "node:assert/strict";
import test from "node:test";

import {
  approvalCardTitle,
  approvalControlKind,
  classifyObservedTurnState,
} from "../src/observations.mjs";

function quiescentObservation(overrides = {}) {
  return {
    busy: false,
    approvalCardCount: 0,
    inputRequired: false,
    failed: false,
    composerVisible: true,
    userTurnCount: 1,
    assistantTurnCount: 1,
    responseControlId: "turn-assistant",
    ...overrides,
  };
}

test("quiescent count parity and an existing response control remain indeterminate", () => {
  assert.equal(
    classifyObservedTurnState(quiescentObservation()),
    "indeterminate",
  );
  assert.equal(
    classifyObservedTurnState(
      quiescentObservation({ userTurnCount: 0, assistantTurnCount: 0 }),
    ),
    "indeterminate",
  );
});

test("direct busy and approval observations remain classified", () => {
  assert.equal(
    classifyObservedTurnState(quiescentObservation({ busy: true })),
    "busy",
  );
  assert.equal(
    classifyObservedTurnState(
      quiescentObservation({ approvalCardCount: 1 }),
    ),
    "approval_required",
  );
});

test("direct input and failure observations remain classified", () => {
  assert.equal(
    classifyObservedTurnState(quiescentObservation({ inputRequired: true })),
    "input_required",
  );
  assert.equal(
    classifyObservedTurnState(quiescentObservation({ failed: true })),
    "failed",
  );
});

test("approval semantics accept nested Allow text and heading-free card titles", () => {
  assert.equal(
    approvalControlKind("Allowfor chatfor this conversation"),
    "allow",
  );
  assert.equal(approvalControlKind("Deny"), "deny");
  assert.equal(approvalControlKind("Allow unrelated action"), "allow");
  assert.equal(approvalControlKind("Continue"), null);
  assert.equal(
    approvalCardTitle("\n  Run Python code?  \npython\nconda env"),
    "Run Python code?",
  );
});
