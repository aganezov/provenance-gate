import assert from "node:assert/strict";
import test from "node:test";

import {
  buildSelectModelSource,
  matchModelOption,
  normalizeVisibleText,
  primaryModelOptionLabel,
  validateModelLabel,
} from "../src/model_selection.mjs";

test("normalizes visible menu text without losing label boundaries", () => {
  assert.equal(
    normalizeVisibleText("  Research Large\nBest for rigor  "),
    "Research Large Best for rigor",
  );
  assert.deepEqual(
    matchModelOption(
      [
        { text: "Research Large\nBest for rigor", checked: true },
        { text: "Research Fast\nBest for speed", checked: false },
      ],
      "Research Fast",
    ),
    {
      index: 1,
      text: "Research Fast Best for speed",
      primaryLabel: "Research Fast",
      checked: false,
    },
  );
  assert.equal(
    primaryModelOptionLabel({ text: "Research Fast\nBest for speed" }),
    "Research Fast",
  );
});

test("scoped top-level fixture selects one exact primary label", () => {
  const observedShape = [
    { text: "Research Large\nBest for rigor", topLevel: true },
    { text: "Research Fast\nBest for speed", topLevel: true },
    { text: "Research Fast\nBest for speed", topLevel: false },
    { text: "Research Small\nLightweight", topLevel: true },
  ];
  assert.equal(matchModelOption(observedShape, "Research Fast").index, 1);
});

test("distinct duplicate and zero exact labels fail before selection", () => {
  let clicks = 0;
  assert.throws(
    () =>
      matchModelOption(
        [
          { text: "Research Fast\nFirst description", topLevel: true },
          { text: "Research Fast\nSecond description", topLevel: true },
        ],
        "Research Fast",
      ),
    /MODEL_OPTION_AMBIGUOUS/,
  );
  assert.throws(
    () => matchModelOption([{ text: "Research Large", topLevel: true }], "Research Fast"),
    /MODEL_OPTION_AMBIGUOUS/,
  );
  assert.equal(clicks, 0);
});

test("rejects malformed labels", () => {
  assert.throws(() => validateModelLabel(" leading"), /invalid/);
  assert.throws(() => validateModelLabel("bad\nlabel"), /invalid/);
  assert.throws(() => validateModelLabel("x".repeat(129)), /invalid/);
});

test("generated source is exact, blank-chat-only, and post-click fail-closed", () => {
  const source = buildSelectModelSource({
    origin: "http://example.invalid:8765",
    projectId: "project-1",
    chatId: "chat-1",
    modelLabel: 'Research "Fast"',
    helpersSource: "() => ({})",
  });
  assert.match(source, /MODEL_SELECTION_REQUIRES_BLANK_CHAT/);
  assert.match(source, /MODEL_OPTION_AMBIGUOUS/);
  assert.match(source, /MODEL_SELECTION_UNCONFIRMED/);
  assert.match(source, /visibleMenuOptions\.first\(\)\.waitFor/);
  assert.match(source, /primaryLabel ===/);
  assert.equal(source.includes('.trim().replace(/\\s+/g, " ")'), true);
  assert.equal(source.includes(".split(/\\r?\\n/)"), true);
  assert.match(
    source,
    /if \(matches\.length !== 1\).*mutationAttempted = true;\s+await matches\[0\]\.click\(\)/s,
  );
  assert.match(source, /Research \\"Fast\\"/);
  assert.doesNotMatch(source, /source_path/);
});
