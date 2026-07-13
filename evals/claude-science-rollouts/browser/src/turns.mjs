import { createHash } from "node:crypto";

import {
  ORIGIN_FROM_HTTP_URL_SOURCE,
  PAGE_OBSERVATION_HELPERS_SOURCE,
  originFromHttpUrl,
} from "./observations.mjs";

export { originFromHttpUrl };

const MAX_DELIVERY_TEXT_BYTES = 65536;
const POLL_INTERVAL_MS = 400;
const STABLE_SAMPLES = 2;

export function normalizeVisibleText(value) {
  if (typeof value !== "string") throw new TypeError("Visible text must be a string");
  return value.normalize("NFC").replace(/\s+/gu, " ").trim();
}

export function sha256Hex(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

export function deliveryTextSha256(value) {
  return sha256Hex(normalizeVisibleText(value));
}

export function composerInsertionText(value) {
  if (typeof value !== "string") throw new TypeError("Composer text must be a string");
  return value.replace(/\r\n|[\n\r\u2028\u2029]/gu, " ");
}

const NORMALIZE_VISIBLE_TEXT_SOURCE = normalizeVisibleText.toString();

const COLLECT_TURN_OBSERVATION_SOURCE = `async (page) => {
  const locationState = await page.evaluate(() => {
    const { parseLocation } = (${PAGE_OBSERVATION_HELPERS_SOURCE})();
    return parseLocation();
  });
  const chatId = await page.evaluate(() => {
    const { activeChatId } = (${PAGE_OBSERVATION_HELPERS_SOURCE})();
    return activeChatId();
  });
  const composer = await page.evaluate(() => {
    const { composerState } = (${PAGE_OBSERVATION_HELPERS_SOURCE})();
    return composerState();
  });
  const turns = await page.evaluate((maximumTextBytes) => {
    const { transcript } = (${PAGE_OBSERVATION_HELPERS_SOURCE})();
    return transcript(maximumTextBytes);
  }, ${MAX_DELIVERY_TEXT_BYTES});
  const cards = await page.evaluate(async () => {
    const { approvalCards } = (${PAGE_OBSERVATION_HELPERS_SOURCE})();
    return approvalCards();
  });
  const signals = await page.evaluate(() => {
    const { turnStateSignals } = (${PAGE_OBSERVATION_HELPERS_SOURCE})();
    return turnStateSignals();
  });
  const frame = locationState.rootFrameId
    ? await page.evaluate((rootFrameId) => {
        const { rootFrameModel } = (${PAGE_OBSERVATION_HELPERS_SOURCE})();
        return rootFrameModel(rootFrameId);
      }, locationState.rootFrameId)
    : null;
  const publicTurns = Array.isArray(turns)
    ? turns.map(({ index: _discardIndex, ...turn }) => turn)
    : turns;
  const assistantTurns = Array.isArray(publicTurns)
    ? publicTurns.filter((turn) => turn.role === "assistant")
    : [];
  return {
    origin: locationState.origin,
    project_id: locationState.projectId,
    chat_id: chatId,
    root_frame_id: locationState.rootFrameId,
    root_project_id: frame?.project_id ?? null,
    composer,
    transcript: publicTurns,
    response_control_id:
      assistantTurns.length > 0
        ? assistantTurns[assistantTurns.length - 1].turn_id
        : null,
    approval_cards: cards,
    signals,
  };
}`;

const POLL_TURN_SOURCE = `async ({
  page,
  expectedOrigin,
  expectedProjectId,
  expectedChatId,
  expectedRootFrameId,
  authoredPromptSha256,
  expectedDeliverySha256,
  normalizedUserTurnId,
  baselineUserTurns,
  baselineResponseControlId,
  rootCreated,
  pollLimitMs,
}) => {
  const collect = ${COLLECT_TURN_OBSERVATION_SOURCE};
  const normalizeVisibleText = ${NORMALIZE_VISIBLE_TEXT_SOURCE};
  const sha256 = async (value) => page.evaluate(async (text) => {
    const bytes = new TextEncoder().encode(text);
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), (byte) =>
      byte.toString(16).padStart(2, "0")).join("");
  }, value);
  const fail = (code) => {
    const error = new Error(code);
    error.boundaryCode = code;
    throw error;
  };
  const verifyIdentity = (observation) => {
    if (observation.origin !== expectedOrigin) fail("NAVIGATION_DRIFT");
    if (
      observation.project_id !== expectedProjectId ||
      observation.chat_id !== expectedChatId ||
      observation.root_frame_id !== expectedRootFrameId ||
      observation.root_project_id !== expectedProjectId
    ) {
      fail("IDENTITY_MISMATCH");
    }
    if (!Array.isArray(observation.approval_cards) || !observation.signals) {
      fail("MALFORMED_BROWSER_STATE");
    }
  };
  const delivery = {
    root_frame_id: expectedRootFrameId,
    authored_prompt_sha256: authoredPromptSha256,
    delivery_text_sha256: expectedDeliverySha256,
    normalized_user_turn_id: normalizedUserTurnId,
  };
  const continuation = {
    project_id: expectedProjectId,
    chat_id: expectedChatId,
    root_frame_id: expectedRootFrameId,
    authored_prompt_sha256: authoredPromptSha256,
    delivery_text_sha256: expectedDeliverySha256,
    normalized_user_turn_id: normalizedUserTurnId,
    baseline_response_control_id: baselineResponseControlId,
  };
  const incomplete = (state, observation, approval = null) => ({
    project_id: expectedProjectId,
    chat_id: expectedChatId,
    root_frame_id: expectedRootFrameId,
    turn_state: state,
    root_created: rootCreated,
    delivery,
    settled: null,
    approval,
    continuation,
    _origin: observation.origin,
  });

  const deadline = Date.now() + pollLimitMs;
  let stableSignature = null;
  let stableSamples = 0;
  while (true) {
    const observation = await collect(page);
    verifyIdentity(observation);
    if (observation.approval_cards.length > 1) fail("AMBIGUOUS_APPROVAL");
    if (!Array.isArray(observation.transcript)) {
      if (observation.approval_cards.length === 1) {
        return incomplete("approval_required", observation, {
          cards: observation.approval_cards,
        });
      }
      // Delivery and all continuation identities were proved before entering
      // this poll. The rooted turn surface may briefly hide both transcript and
      // approval content while it hydrates. Poll read-only within the existing
      // deadline; if it remains unavailable, return the proven continuation to
      // Python instead of losing delivery evidence or replaying the submit.
      if (Date.now() >= deadline) {
        return incomplete(
          observation.signals.busy ? "busy" : "indeterminate",
          observation,
        );
      }
      await page.waitForTimeout(400);
      continue;
    }
    const users = observation.transcript.filter((turn) => turn.role === "user");
    // Cost note: this re-hashes every baseline user turn each poll cycle — up to one
    // page.evaluate round-trip per baseline turn (bounded at 256) per ~400ms interval.
    // For the bare PBMC replicate the baseline is a handful of turns, so this is cheap;
    // a long prior transcript would make each cycle proportionally heavier.
    for (const baseline of baselineUserTurns) {
      const current = users.find((turn) => turn.turn_id === baseline.turn_id);
      if (!current || await sha256(normalizeVisibleText(current.text)) !== baseline.sha256) {
        fail("DELIVERY_MISMATCH");
      }
    }
    const delivered = users.find(
      (turn) => turn.turn_id === normalizedUserTurnId,
    );
    if (!delivered) fail("DELIVERY_MISMATCH");
    if (await sha256(normalizeVisibleText(delivered.text)) !== expectedDeliverySha256) {
      fail("DELIVERY_MISMATCH");
    }
    if (users[users.length - 1]?.turn_id !== normalizedUserTurnId) {
      fail("DELIVERY_MISMATCH");
    }
    if (observation.approval_cards.length === 1) {
      return incomplete("approval_required", observation, {
        cards: observation.approval_cards,
      });
    }
    if (observation.signals.inputRequired) {
      return incomplete("input_required", observation);
    }
    if (observation.signals.failed) {
      return incomplete("failed", observation);
    }

    const responseControlId = observation.response_control_id;
    const isNewResponse =
      responseControlId !== null && responseControlId !== baselineResponseControlId;
    if (!observation.signals.busy && isNewResponse) {
      const assistant = observation.transcript.find(
        (turn) => turn.turn_id === responseControlId && turn.role === "assistant",
      );
      if (!assistant) fail("MALFORMED_BROWSER_STATE");
      const signature = [
        responseControlId,
        observation.transcript.length,
        await sha256(normalizeVisibleText(assistant.text)),
      ].join(":");
      if (signature === stableSignature) stableSamples += 1;
      else {
        stableSignature = signature;
        stableSamples = 1;
      }
      if (stableSamples >= ${STABLE_SAMPLES}) {
        return {
          project_id: expectedProjectId,
          chat_id: expectedChatId,
          root_frame_id: expectedRootFrameId,
          turn_state: "settled",
          root_created: rootCreated,
          delivery,
          settled: {
            stop_hidden: true,
            stable_samples: stableSamples,
            new_response_control_id: responseControlId,
          },
          approval: null,
          continuation: null,
          _origin: observation.origin,
        };
      }
    } else {
      stableSignature = null;
      stableSamples = 0;
    }
    if (Date.now() >= deadline) {
      return incomplete(
        observation.signals.busy ? "busy" : "indeterminate",
        observation,
      );
    }
    await page.waitForTimeout(${POLL_INTERVAL_MS});
  }
}`;

function boundedPollMs(deadlineMs) {
  return Math.max(250, Math.min(30000, deadlineMs - 1000));
}

export function buildSubmitTurnSource({
  origin,
  projectId,
  chatId,
  rootMode,
  rootFrameId,
  prompt,
  authoredPromptSha256,
  expectedDeliverySha256,
  deadlineMs,
}) {
  const insertionText = composerInsertionText(prompt);
  return `async (page) => {
    let mutationAttempted = false;
    const originFromHttpUrl = ${ORIGIN_FROM_HTTP_URL_SOURCE};
    const collect = ${COLLECT_TURN_OBSERVATION_SOURCE};
    const normalizeVisibleText = ${NORMALIZE_VISIBLE_TEXT_SOURCE};
    const sha256 = async (value) => page.evaluate(async (text) => {
      const bytes = new TextEncoder().encode(text);
      const digest = await crypto.subtle.digest("SHA-256", bytes);
      return Array.from(new Uint8Array(digest), (byte) =>
        byte.toString(16).padStart(2, "0")).join("");
    }, value);
    const fail = (code) => {
      const error = new Error(code);
      error.boundaryCode = code;
      throw error;
    };
    try {
      const operationDeadline = Date.now() + ${boundedPollMs(deadlineMs)};
      const before = await collect(page);
      if (before.origin !== ${JSON.stringify(origin)}) fail("NAVIGATION_DRIFT");
      if (
        before.project_id !== ${JSON.stringify(projectId)} ||
        before.chat_id !== ${JSON.stringify(chatId)} ||
        !Array.isArray(before.transcript) ||
        !before.composer ||
        before.composer.empty !== true
      ) fail("IDENTITY_MISMATCH");
      const rootMode = ${JSON.stringify(rootMode)};
      const requestedRoot = ${JSON.stringify(rootFrameId ?? null)};
      if (
        (rootMode === "new" && before.root_frame_id !== null) ||
        (rootMode === "existing" &&
          (before.root_frame_id !== requestedRoot ||
            before.root_project_id !== ${JSON.stringify(projectId)}))
      ) fail("IDENTITY_MISMATCH");

      const baselineUsers = before.transcript
        .filter((turn) => turn.role === "user")
        .map((turn) => ({
          turn_id: turn.turn_id,
          text: turn.text,
        }));
      const baselineUserTurns = [];
      for (const turn of baselineUsers) {
        baselineUserTurns.push({
          turn_id: turn.turn_id,
          sha256: await sha256(normalizeVisibleText(turn.text)),
        });
      }
      const composer = page.locator(
        '[data-testid="composer"] [role="textbox"], [data-testid="composer"] [contenteditable="true"]',
      );
      if (await composer.count() !== 1 || !await composer.isVisible()) {
        fail("MALFORMED_BROWSER_STATE");
      }
      await composer.click();
      const inserted = await page.evaluate((text) => {
        const target = document.activeElement;
        // The composer locator accepts either a role=textbox element or a bare
        // contenteditable div, so the insertion guard must accept both — otherwise a
        // valid contenteditable composer is rejected as MALFORMED_BROWSER_STATE.
        if (
          !(target instanceof HTMLElement) ||
          (target.getAttribute("role") !== "textbox" && !target.isContentEditable)
        ) {
          return false;
        }
        return document.execCommand("insertText", false, text);
      }, ${JSON.stringify(insertionText)});
      if (!inserted) fail("MALFORMED_BROWSER_STATE");
      const send = page.getByRole("button", { name: "Send", exact: true });
      await send.waitFor({ state: "visible", timeout: 5000 });
      if (await send.count() !== 1) fail("MALFORMED_BROWSER_STATE");
      mutationAttempted = true;
      await send.click();

      const deliveryDeadline = Math.min(operationDeadline, Date.now() + 15000);
      let deliveredTurn = null;
      let observedRoot = requestedRoot;
      while (Date.now() <= deliveryDeadline) {
        const current = await collect(page);
        if (current.origin !== ${JSON.stringify(origin)}) fail("NAVIGATION_DRIFT");
        if (current.project_id !== ${JSON.stringify(projectId)}) {
          fail("IDENTITY_MISMATCH");
        }
        if (
          current.chat_id !== null &&
          current.chat_id !== ${JSON.stringify(chatId)}
        ) fail("IDENTITY_MISMATCH");
        if (observedRoot === null && current.root_frame_id !== null) {
          observedRoot = current.root_frame_id;
        }
        if (observedRoot !== null && current.root_frame_id !== observedRoot) {
          fail("IDENTITY_MISMATCH");
        }
        if (
          current.root_project_id !== null &&
          current.root_project_id !== ${JSON.stringify(projectId)}
        ) fail("IDENTITY_MISMATCH");
        // A new-root navigation can expose the exact URL before the active chat,
        // root model, and transcript hydrate. Keep this reconciliation read-only
        // and bounded by the delivery deadline; any observed conflicting identity
        // still fails immediately, and delivery is not accepted until all three
        // exact identities plus the transcript are available together.
        if (
          current.chat_id === null ||
          current.root_project_id === null ||
          !Array.isArray(current.transcript)
        ) {
          await page.waitForTimeout(100);
          continue;
        }
        const users = current.transcript.filter((turn) => turn.role === "user");
        for (const baseline of baselineUserTurns) {
          const match = users.find((turn) => turn.turn_id === baseline.turn_id);
          if (!match || await sha256(normalizeVisibleText(match.text)) !== baseline.sha256) {
            fail("DELIVERY_MISMATCH");
          }
        }
        const baselineIds = new Set(baselineUserTurns.map((turn) => turn.turn_id));
        const appended = users.filter((turn) => !baselineIds.has(turn.turn_id));
        if (appended.length > 1) fail("DELIVERY_MISMATCH");
        if (
          appended.length === 1 &&
          observedRoot !== null &&
          current.root_project_id === ${JSON.stringify(projectId)}
        ) {
          const hash = await sha256(normalizeVisibleText(appended[0].text));
          if (hash !== ${JSON.stringify(expectedDeliverySha256)}) {
            fail("DELIVERY_MISMATCH");
          }
          deliveredTurn = appended[0];
          break;
        }
        await page.waitForTimeout(100);
      }
      if (!deliveredTurn || observedRoot === null) fail("DELIVERY_NOT_CONFIRMED");
      const poll = ${POLL_TURN_SOURCE};
      const result = await poll({
        page,
        expectedOrigin: ${JSON.stringify(origin)},
        expectedProjectId: ${JSON.stringify(projectId)},
        expectedChatId: ${JSON.stringify(chatId)},
        expectedRootFrameId: observedRoot,
        authoredPromptSha256: ${JSON.stringify(authoredPromptSha256)},
        expectedDeliverySha256: ${JSON.stringify(expectedDeliverySha256)},
        normalizedUserTurnId: deliveredTurn.turn_id,
        baselineUserTurns,
        baselineResponseControlId: before.response_control_id,
        rootCreated: rootMode === "new",
        pollLimitMs: Math.max(0, operationDeadline - Date.now()),
      });
      const { _origin: resultOrigin, ...publicResult } = result;
      return {
        _origin: resultOrigin,
        _mutation_attempted: true,
        result: publicResult,
      };
    } catch (error) {
      return {
        _origin: originFromHttpUrl(page.url()),
        _mutation_attempted: mutationAttempted,
        _boundary_error: error?.boundaryCode ?? "MALFORMED_BROWSER_STATE",
      };
    }
  }`;
}

export function buildWaitTurnSource({ origin, continuation, deadlineMs }) {
  return `async (page) => {
    const originFromHttpUrl = ${ORIGIN_FROM_HTTP_URL_SOURCE};
    try {
      const collect = ${COLLECT_TURN_OBSERVATION_SOURCE};
      const before = await collect(page);
      if (before.origin !== ${JSON.stringify(origin)}) {
        return { _origin: before.origin, _boundary_error: "NAVIGATION_DRIFT" };
      }
      if (
        before.project_id !== ${JSON.stringify(continuation.project_id)} ||
        before.chat_id !== ${JSON.stringify(continuation.chat_id)} ||
        before.root_frame_id !== ${JSON.stringify(continuation.root_frame_id)} ||
        before.root_project_id !== ${JSON.stringify(continuation.project_id)}
      ) {
        return { _origin: before.origin, _boundary_error: "IDENTITY_MISMATCH" };
      }
      const poll = ${POLL_TURN_SOURCE};
      const result = await poll({
        page,
        expectedOrigin: ${JSON.stringify(origin)},
        expectedProjectId: ${JSON.stringify(continuation.project_id)},
        expectedChatId: ${JSON.stringify(continuation.chat_id)},
        expectedRootFrameId: ${JSON.stringify(continuation.root_frame_id)},
        authoredPromptSha256: ${JSON.stringify(continuation.authored_prompt_sha256)},
        expectedDeliverySha256: ${JSON.stringify(continuation.delivery_text_sha256)},
        normalizedUserTurnId: ${JSON.stringify(continuation.normalized_user_turn_id)},
        baselineUserTurns: [],
        baselineResponseControlId: ${JSON.stringify(continuation.baseline_response_control_id)},
        rootCreated: false,
        pollLimitMs: ${boundedPollMs(deadlineMs)},
      });
      const { _origin: resultOrigin, ...publicResult } = result;
      return { _origin: resultOrigin, result: publicResult };
    } catch (error) {
      return {
        _origin: originFromHttpUrl(page.url()),
        _boundary_error: error?.boundaryCode ?? "MALFORMED_BROWSER_STATE",
      };
    }
  }`;
}

export function buildResolveApprovalSource({
  origin,
  projectId,
  chatId,
  rootFrameId,
  cardId,
  decision,
  expectedFingerprint,
}) {
  return `async (page) => {
    let mutationAttempted = false;
    const originFromHttpUrl = ${ORIGIN_FROM_HTTP_URL_SOURCE};
    const collect = ${COLLECT_TURN_OBSERVATION_SOURCE};
    try {
      const before = await collect(page);
      if (before.origin !== ${JSON.stringify(origin)}) {
        return { _origin: before.origin, _boundary_error: "NAVIGATION_DRIFT" };
      }
      if (
        before.project_id !== ${JSON.stringify(projectId)} ||
        before.chat_id !== ${JSON.stringify(chatId)} ||
        before.root_frame_id !== ${JSON.stringify(rootFrameId)} ||
        before.root_project_id !== ${JSON.stringify(projectId)}
      ) {
        return { _origin: before.origin, _boundary_error: "IDENTITY_MISMATCH" };
      }
      if (
        !Array.isArray(before.approval_cards) ||
        before.approval_cards.length !== 1 ||
        before.approval_cards[0].card_id !== ${JSON.stringify(cardId)} ||
        before.approval_cards[0].fingerprint !== ${JSON.stringify(expectedFingerprint)}
      ) {
        return { _origin: before.origin, _boundary_error: "AMBIGUOUS_APPROVAL" };
      }
      const cards = page.locator('[data-testid="approval-card"]:visible');
      if (await cards.count() !== 1) {
        return { _origin: before.origin, _boundary_error: "AMBIGUOUS_APPROVAL" };
      }
      const card = cards.nth(0);
      const control = ${JSON.stringify(decision)} === "allow_for_conversation"
        ? card.getByRole("button", {
            name: /^Allow(?:\\s+for chat)?\\s+for this conversation$/i,
          })
        : card.getByRole("button", { name: /^Deny$/i });
      const deny = card.getByRole("button", { name: /^Deny$/i });
      if (await control.count() !== 1 || await deny.count() !== 1) {
        return { _origin: before.origin, _boundary_error: "AMBIGUOUS_APPROVAL" };
      }
      mutationAttempted = true;
      await control.click();
      await control.waitFor({ state: "hidden", timeout: 5000 });
      const after = await collect(page);
      if (after.origin !== ${JSON.stringify(origin)}) {
        throw Object.assign(new Error("NAVIGATION_DRIFT"), {
          boundaryCode: "NAVIGATION_DRIFT",
        });
      }
      if (
        after.project_id !== ${JSON.stringify(projectId)} ||
        after.chat_id !== ${JSON.stringify(chatId)} ||
        after.root_frame_id !== ${JSON.stringify(rootFrameId)} ||
        after.root_project_id !== ${JSON.stringify(projectId)} ||
        !Array.isArray(after.approval_cards) ||
        after.approval_cards.some((item) =>
          item.card_id === ${JSON.stringify(cardId)} ||
          item.fingerprint === ${JSON.stringify(expectedFingerprint)})
      ) {
        throw Object.assign(new Error("APPROVAL_NOT_CLEARED"), {
          boundaryCode: "APPROVAL_NOT_CLEARED",
        });
      }
      return {
        _origin: after.origin,
        _mutation_attempted: true,
        result: {
          project_id: ${JSON.stringify(projectId)},
          chat_id: ${JSON.stringify(chatId)},
          root_frame_id: ${JSON.stringify(rootFrameId)},
          card_id: ${JSON.stringify(cardId)},
          decision: ${JSON.stringify(decision)},
          verified_cleared: true,
        },
      };
    } catch (error) {
      return {
        _origin: originFromHttpUrl(page.url()),
        _mutation_attempted: mutationAttempted,
        _boundary_error: error?.boundaryCode ?? "APPROVAL_NOT_CLEARED",
      };
    }
  }`;
}
