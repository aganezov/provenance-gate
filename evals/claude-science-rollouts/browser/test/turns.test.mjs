import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  buildSubmitTurnSource,
  buildResolveApprovalSource,
  buildWaitTurnSource,
  composerInsertionText,
  deliveryTextSha256,
  normalizeVisibleText,
  originFromHttpUrl,
  sha256Hex,
} from "../src/turns.mjs";

const scenario = JSON.parse(
  readFileSync(new URL("../../scenarios/pbmc_figure_package.json", import.meta.url)),
);
const vectors = JSON.parse(
  readFileSync(new URL("./fixtures/delivery_hash_vectors.json", import.meta.url)),
);

function promptById() {
  return new Map([
    ...scenario.construction.map((turn) => [turn.turn_id, turn.prompt]),
    ["assemble-final-bare", scenario.trial.variants.bare],
    ...scenario.response_rules.map((rule) => [rule.id, rule.reply]),
  ]);
}

test("visible-text normalization has frozen PBMC delivery vectors", () => {
  assert.equal(
    normalizeVisibleText("  alpha\r\n beta\t gamma  "),
    "alpha beta gamma",
  );
  const prompts = promptById();
  assert.equal(vectors.vectors.length, prompts.size);
  for (const vector of vectors.vectors) {
    const prompt = prompts.get(vector.id);
    assert.equal(typeof prompt, "string");
    assert.equal(sha256Hex(prompt), vector.authored_prompt_sha256);
    assert.equal(deliveryTextSha256(prompt), vector.delivery_text_sha256);
  }
});

test("submit source contains one send and wait source cannot submit", () => {
  const submit = buildSubmitTurnSource({
    origin: "http://127.0.0.1:8875",
    projectId: "project-001",
    chatId: "chat-001",
    rootMode: "new",
    rootFrameId: null,
    prompt: "Do one thing",
    authoredPromptSha256: "a".repeat(64),
    expectedDeliverySha256: "b".repeat(64),
    deadlineMs: 15000,
  });
  assert.doesNotThrow(() => new Function(`return (${submit})`));
  assert.equal(submit.match(/await send\.click\(\)/g)?.length, 1);
  assert.equal(submit.match(/execCommand\("insertText"/g)?.length, 1);
  const wait = buildWaitTurnSource({
    origin: "http://127.0.0.1:8875",
    continuation: {
      project_id: "project-001",
      chat_id: "chat-001",
      root_frame_id: "root-001",
      authored_prompt_sha256: "a".repeat(64),
      delivery_text_sha256: "b".repeat(64),
      normalized_user_turn_id: "turn-user",
      baseline_response_control_id: null,
    },
    deadlineMs: 15000,
  });
  assert.doesNotThrow(() => new Function(`return (${wait})`));
  assert.doesNotMatch(wait, /insertText|\bSend\b|\.click\(/);
  assert.match(wait, /waitForTimeout\(400\)/);

  const approval = buildResolveApprovalSource({
    origin: "http://127.0.0.1:8875",
    projectId: "project-001",
    chatId: "chat-001",
    rootFrameId: "root-001",
    cardId: "approval:abc:0",
    decision: "allow_for_conversation",
    expectedFingerprint: "c".repeat(64),
  });
  assert.doesNotThrow(() => new Function(`return (${approval})`));
  assert.equal(approval.match(/await control\.click\(\)/g)?.length, 1);
  assert.match(approval, /approval-card/);
  assert.match(approval, /Allow\(\?:\\s\+for chat\)\?\\s\+for this conversation/);
  assert.match(approval, /Deny/);
});

test("submit insertion guard accepts a contenteditable composer, not only role=textbox", () => {
  // Regression for selector/guard skew: the composer locator admits either a
  // role=textbox element or a bare contenteditable div, so the insertion guard must
  // accept both. The prior guard checked role=textbox alone and silently rejected a
  // valid contenteditable composer as MALFORMED_BROWSER_STATE.
  const submit = buildSubmitTurnSource({
    origin: "http://127.0.0.1:8875",
    projectId: "project-001",
    chatId: "chat-001",
    rootMode: "new",
    rootFrameId: null,
    prompt: "Do one thing",
    authoredPromptSha256: "a".repeat(64),
    expectedDeliverySha256: "b".repeat(64),
    deadlineMs: 15000,
  });
  // The locator admits a contenteditable composer...
  assert.match(submit, /\[contenteditable="true"\]/);
  // ...and the insertion guard must accept it too, not only role=textbox.
  assert.match(submit, /isContentEditable/);
});

test("composer insertion preserves a visible separator across line boundaries", () => {
  const prompt = "first line\nsecond line\r\nthird line";
  const insertion = composerInsertionText(prompt);
  assert.equal(insertion, "first line second line third line");
  assert.equal(deliveryTextSha256(insertion), deliveryTextSha256(prompt));

  const submit = buildSubmitTurnSource({
    origin: "http://127.0.0.1:8875",
    projectId: "project-001",
    chatId: "chat-001",
    rootMode: "new",
    rootFrameId: null,
    prompt,
    authoredPromptSha256: sha256Hex(prompt),
    expectedDeliverySha256: deliveryTextSha256(prompt),
    deadlineMs: 15000,
  });
  assert.match(submit, /first line second line third line/);
  assert.doesNotMatch(submit, /first line\\nsecond line/);
});

test("generated catch paths derive origin without VM URL globals", () => {
  assert.equal(
    originFromHttpUrl("http://localhost:8875/projects/project-001"),
    "http://localhost:8875",
  );
  assert.equal(
    originFromHttpUrl("https://[::1]:8875/projects/project-001"),
    "https://[::1]:8875",
  );
  const submit = buildSubmitTurnSource({
    origin: "http://localhost:8875",
    projectId: "project-001",
    chatId: "chat-001",
    rootMode: "new",
    rootFrameId: null,
    prompt: "Do one thing",
    authoredPromptSha256: "a".repeat(64),
    expectedDeliverySha256: "b".repeat(64),
    deadlineMs: 15000,
  });
  assert.doesNotMatch(submit, /new URL\(page\.url\(\)\)/);
  assert.match(submit, /originFromHttpUrl\(page\.url\(\)\)/);
});

test("one submit callback crosses root navigation and returns approval to the caller", async () => {
  const origin = "http://localhost:8875";
  const projectId = "project-001";
  const chatId = "chat-001";
  const rootId = "root-001";
  const prompt = "first line\nsecond line";
  const visiblePrompt = composerInsertionText(prompt);
  const card = {
    card_id: "approval:abc:0",
    fingerprint: "c".repeat(64),
    title: "Run code?",
    kind: "approval",
  };
  const source = buildSubmitTurnSource({
    origin,
    projectId,
    chatId,
    rootMode: "new",
    rootFrameId: null,
    prompt,
    authoredPromptSha256: sha256Hex(prompt),
    expectedDeliverySha256: deliveryTextSha256(prompt),
    deadlineMs: 15000,
  });
  let observationIndex = -1;
  let submitted = false;
  let sendCount = 0;
  const page = {
    url: () => submitted
      ? `${origin}/projects/${projectId}/frames/${rootId}`
      : `${origin}/projects/${projectId}`,
    async evaluate(fn, argument) {
      const body = fn.toString();
      if (body.startsWith("async (text)")) return sha256Hex(argument);
      if (body.startsWith("(text)")) return true;
      if (body.includes("return parseLocation();")) {
        observationIndex += 1;
        return {
          origin,
          projectId,
          rootFrameId: observationIndex === 0 ? null : rootId,
        };
      }
      if (body.includes("return activeChatId();")) {
        return observationIndex === 1 ? null : chatId;
      }
      if (body.includes("return composerState();")) {
        return { empty: !submitted, visible: true };
      }
      if (body.includes("return transcript(maximumTextBytes);")) {
        if (observationIndex === 1 || observationIndex >= 3) return null;
        return observationIndex === 0
          ? []
          : [{
              index: 0,
              turn_id: "user-001",
              role: "user",
              text: visiblePrompt,
              truncated: false,
            }];
      }
      if (body.includes("return approvalCards();")) {
        return observationIndex >= 4 ? [card] : [];
      }
      if (body.includes("return turnStateSignals();")) {
        return { busy: true, failed: false, inputRequired: false };
      }
      if (body.includes("return rootFrameModel(rootFrameId);")) {
        return observationIndex === 1 ? null : { project_id: projectId };
      }
      assert.fail(`Unexpected page.evaluate callback: ${body.slice(0, 80)}`);
    },
    locator() {
      return {
        count: async () => 1,
        isVisible: async () => true,
        click: async () => {},
      };
    },
    getByRole(role, options) {
      assert.equal(role, "button");
      assert.equal(options.name, "Send");
      return {
        waitFor: async () => {},
        count: async () => 1,
        async click() {
          sendCount += 1;
          submitted = true;
        },
      };
    },
    async waitForTimeout() {},
  };
  const run = new Function(`return (${source})`)();

  const wrapper = await run(page);

  assert.equal(sendCount, 1);
  assert.equal(wrapper._mutation_attempted, true);
  assert.equal(wrapper.result.root_frame_id, rootId);
  assert.equal(wrapper.result.root_created, true);
  assert.equal(wrapper.result.turn_state, "approval_required");
  assert.deepEqual(wrapper.result.approval.cards, [card]);
  assert.equal(wrapper.result.continuation.normalized_user_turn_id, "user-001");
});

test("resumed polling rejects a later user turn before settlement", async () => {
  const prompt = "Target prompt";
  const source = buildWaitTurnSource({
    origin: "http://127.0.0.1:8875",
    continuation: {
      project_id: "project-001",
      chat_id: "chat-001",
      root_frame_id: "root-001",
      authored_prompt_sha256: sha256Hex(prompt),
      delivery_text_sha256: deliveryTextSha256(prompt),
      normalized_user_turn_id: "turn-user-target",
      baseline_response_control_id: "turn-assistant-old",
    },
    deadlineMs: 15000,
  });
  const transcript = [
    {
      turn_id: "turn-user-target",
      role: "user",
      text: prompt,
      truncated: false,
    },
    {
      turn_id: "turn-user-later",
      role: "user",
      text: "Later prompt",
      truncated: false,
    },
    {
      turn_id: "turn-assistant-later",
      role: "assistant",
      text: "Later answer",
      truncated: false,
    },
  ];
  let observationRead = 0;
  const page = {
    url: () => "http://127.0.0.1:8875/projects/project-001",
    async evaluate(fn, argument) {
      const body = fn.toString();
      if (body.startsWith("async (text)")) return sha256Hex(argument);
      const field = observationRead % 7;
      observationRead += 1;
      if (field === 0) {
        return {
          origin: "http://127.0.0.1:8875",
          projectId: "project-001",
          rootFrameId: "root-001",
        };
      }
      if (field === 1) return "chat-001";
      if (field === 2) return { empty: true, visible: true };
      if (field === 3) return transcript;
      if (field === 4) return [];
      if (field === 5) return { busy: false, failed: false, inputRequired: false };
      return { project_id: "project-001" };
    },
    async waitForTimeout() {
      assert.fail("Polling must fail closed before waiting");
    },
  };
  const run = new Function(`return (${source})`)();

  const result = await run(page);

  assert.equal(result._boundary_error, "DELIVERY_MISMATCH");
  assert.equal(result.result, undefined);
});
