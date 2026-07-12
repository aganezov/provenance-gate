import { BoundaryError } from "./protocol.mjs";
import { runCli } from "./cli_runner.mjs";
import {
  buildChatInspectSource,
  buildContextInspectSource,
  buildProjectInspectSource,
} from "./observations.mjs";
import {
  buildResolveApprovalSource,
  buildSubmitTurnSource,
  buildWaitTurnSource,
  deliveryTextSha256,
  sha256Hex,
} from "./turns.mjs";

const SESSION_NAME = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;

export const SESSION_INSPECT_SOURCE = `async (page) => {
  const pageState = await page.evaluate(() => ({
    bodyPresent: document.body !== null,
    hostname: location.hostname,
    origin: location.origin,
    readyState: document.readyState,
  }));
  const storage = await page.context().storageState();
  const cookieMatches = storage.cookies.some((item) => {
    const domain = item.domain.replace(/^\\./, "");
    return pageState.hostname === domain || pageState.hostname.endsWith("." + domain);
  });
  const localStateMatches = storage.origins.some((item) =>
    item.origin === pageState.origin && item.localStorage.length > 0
  );
  return {
    authenticated: cookieMatches || localStateMatches,
    origin: pageState.origin,
    profile_ready:
      pageState.bodyPresent && ["interactive", "complete"].includes(pageState.readyState),
  };
}`;

export function createSessionAttachHandler({
  browserOwner,
  runCommand = runCli,
} = {}) {
  return async function attachSession(_payload, context) {
    const startedAt = performance.now();
    if (browserOwner === undefined) {
      throw new BoundaryError(
        "BROWSER_OWNER_REQUIRED",
        "Browser owner is not configured",
      );
    }
    if (!SESSION_NAME.test(browserOwner)) {
      throw new BoundaryError(
        "INVALID_BROWSER_OWNER",
        "Configured browser owner is invalid",
      );
    }
    try {
      await runCommand(
        [
          "--raw",
          "attach",
          browserOwner,
          "--session",
          context.session.session_id,
        ],
        { deadlineMs: remainingDeadline(context.deadlineMs, startedAt) },
      );
    } catch (error) {
      throw mutationFailure("ATTACH", error);
    }
    try {
      return await inspectSession(runCommand, context, startedAt);
    } catch (error) {
      const cleanedUp = await tryDetachSession(runCommand, context, startedAt);
      if (!cleanedUp) {
        throw new BoundaryError(
          "ATTACH_CLEANUP_UNCONFIRMED",
          "Browser session attachment cleanup could not be confirmed",
          {
            outcome: "unknown_outcome",
            retryable: false,
            evidence: {
              cause_code: error instanceof BoundaryError
                ? error.code
                : "BOUNDARY_FAILURE",
            },
          },
        );
      }
      throw error;
    }
  };
}

export function createSessionInspectHandler({ runCommand = runCli } = {}) {
  return async function inspectExistingSession(_payload, context) {
    return inspectSession(runCommand, context, performance.now());
  };
}

export function createSessionDetachHandler({ runCommand = runCli } = {}) {
  return async function detachSession(_payload, context) {
    let stdout;
    try {
      stdout = await runCommand(
        ["--json", `-s=${context.session.session_id}`, "detach"],
        { deadlineMs: context.deadlineMs },
      );
    } catch (error) {
      throw mutationFailure("DETACH", error);
    }
    let result;
    try {
      result = parseCliJson(stdout);
    } catch (error) {
      throw mutationFailure("DETACH", error);
    }
    if (
      result?.session !== context.session.session_id ||
      result?.status !== "detached"
    ) {
      throw new BoundaryError(
        "DETACH_OUTCOME_UNKNOWN",
        "Browser session detach outcome is unknown",
        {
          outcome: "unknown_outcome",
          retryable: false,
          evidence: { cause_code: "UNCONFIRMED_RESULT" },
        },
      );
    }
    return { detached: true };
  };
}

export function createProjectInspectHandler({ runCommand = runCli } = {}) {
  return async function inspectProject(payload, context) {
    const result = await runObservation(
      runCommand,
      buildProjectInspectSource(payload.project_id),
      context,
    );
    verifyOrigin(result, context);
    if (
      result.project_id !== payload.project_id ||
      result.verified !== true ||
      result.composer_empty === null ||
      result.user_turn_count === null ||
      (result.root_frame_id !== null &&
        result._root_project_id !== payload.project_id)
    ) {
      throw identityMismatch("Project observation identity is inconsistent");
    }
    const {
      _origin: _discardOrigin,
      _root_project_id: _discardRootProject,
      ...observation
    } = result;
    return observation;
  };
}

export function createChatInspectHandler({ runCommand = runCli } = {}) {
  return async function inspectChat(payload, context) {
    const result = await runObservation(
      runCommand,
      buildChatInspectSource(
        payload.project_id,
        payload.chat_id,
        payload.root_frame_id ?? null,
      ),
      context,
    );
    verifyOrigin(result, context);
    const expectedRoot = payload.root_frame_id ?? null;
    if (
      result.project_id !== payload.project_id ||
      result.chat_id !== payload.chat_id ||
      result.root_frame_id !== expectedRoot ||
      (result.root_frame_id !== null &&
        result._root_project_id !== payload.project_id)
    ) {
      throw identityMismatch("Chat observation identity is inconsistent");
    }
    if (
      !Array.isArray(result.transcript) ||
      !Array.isArray(result.approval_cards) ||
      result.composer_empty === null ||
      result.user_turn_count === null ||
      result.current_turn_state === null
    ) {
      throw new BoundaryError(
        "AMBIGUOUS_BROWSER_STATE",
        "Chat observation could not be classified",
      );
    }
    const {
      _origin: _discardOrigin,
      _expected: _discardExpected,
      _root_project_id: _discardRootProject,
      ...observation
    } = result;
    return observation;
  };
}

export function createContextInspectHandler({ runCommand = runCli } = {}) {
  return async function inspectContext(payload, context) {
    const result = await runObservation(
      runCommand,
      buildContextInspectSource(payload.project_id),
      context,
    );
    verifyOrigin(result, context);
    if (
      result.project_id !== payload.project_id ||
      !Array.isArray(result.enabled_skills) ||
      typeof result.context_hash !== "string"
    ) {
      throw identityMismatch("Context observation identity is inconsistent");
    }
    const { _origin: _discardOrigin, ...observation } = result;
    return observation;
  };
}

export function createSubmitTurnWaitHandler({ runCommand = runCli } = {}) {
  return async function submitTurnWait(payload, context) {
    if (sha256Hex(payload.prompt) !== payload.authored_prompt_sha256) {
      throw new BoundaryError(
        "PROMPT_HASH_MISMATCH",
        "Raw prompt bytes do not match authored_prompt_sha256",
      );
    }
    const expectedDeliverySha256 = deliveryTextSha256(payload.prompt);
    let wrapper;
    try {
      wrapper = await runBoundaryCode(
        runCommand,
        buildSubmitTurnSource({
          origin: context.session.origin,
          projectId: payload.project_id,
          chatId: payload.chat_id,
          rootMode: payload.root_mode,
          rootFrameId: payload.root_frame_id ?? null,
          prompt: payload.prompt,
          authoredPromptSha256: payload.authored_prompt_sha256,
          expectedDeliverySha256,
          deadlineMs: context.deadlineMs,
        }),
        context,
      );
    } catch (error) {
      throw mutationFailure("SUBMIT", error);
    }
    return unwrapTurnResult(wrapper, context, {
      projectId: payload.project_id,
      chatId: payload.chat_id,
      rootFrameId: payload.root_frame_id ?? null,
      rootMode: payload.root_mode,
      authoredPromptSha256: payload.authored_prompt_sha256,
      deliveryTextSha256: expectedDeliverySha256,
      mutationAction: "SUBMIT",
    });
  };
}

export function createWaitTurnHandler({ runCommand = runCli } = {}) {
  return async function waitTurn(payload, context) {
    const continuation = payload.continuation;
    const wrapper = await runBoundaryCode(
      runCommand,
      buildWaitTurnSource({
        origin: context.session.origin,
        continuation,
        deadlineMs: context.deadlineMs,
      }),
      context,
    );
    return unwrapTurnResult(wrapper, context, {
      projectId: payload.project_id,
      chatId: payload.chat_id,
      rootFrameId: continuation.root_frame_id,
      rootMode: "existing",
      authoredPromptSha256: continuation.authored_prompt_sha256,
      deliveryTextSha256: continuation.delivery_text_sha256,
    });
  };
}

export function createResolveApprovalHandler({ runCommand = runCli } = {}) {
  return async function resolveApproval(payload, context) {
    let wrapper;
    try {
      wrapper = await runBoundaryCode(
        runCommand,
        buildResolveApprovalSource({
          origin: context.session.origin,
          projectId: payload.project_id,
          chatId: payload.chat_id,
          rootFrameId: payload.root_frame_id,
          cardId: payload.card_id,
          decision: payload.decision,
          expectedFingerprint: payload.expected_fingerprint,
        }),
        context,
      );
    } catch (error) {
      throw mutationFailure("APPROVAL", error);
    }
    if (
      wrapper._origin !== context.session.origin &&
      wrapper._mutation_attempted !== false
    ) {
      throw mutationFailure(
        "APPROVAL",
        new BoundaryError("NAVIGATION_DRIFT", "Approval outcome is unknown"),
      );
    }
    verifyOrigin(wrapper, context);
    if (wrapper._boundary_error) {
      if (wrapper._mutation_attempted) {
        throw mutationFailure(
          "APPROVAL",
          new BoundaryError(wrapper._boundary_error, "Approval outcome is unknown"),
        );
      }
      throw browserStateError(wrapper._boundary_error);
    }
    const result = wrapper.result;
    if (
      !result ||
      result.project_id !== payload.project_id ||
      result.chat_id !== payload.chat_id ||
      result.root_frame_id !== payload.root_frame_id ||
      result.card_id !== payload.card_id ||
      result.decision !== payload.decision ||
      result.verified_cleared !== true
    ) {
      throw mutationFailure(
        "APPROVAL",
        new BoundaryError(
          "IDENTITY_MISMATCH",
          "Approval result identity is inconsistent",
        ),
      );
    }
    return result;
  };
}

async function runBoundaryCode(runCommand, source, context) {
  const stdout = await runCommand(
    ["--raw", `-s=${context.session.session_id}`, "run-code", source],
    { deadlineMs: context.deadlineMs },
  );
  const result = parseCliJson(stdout);
  if (result === null || typeof result !== "object" || Array.isArray(result)) {
    throw new BoundaryError(
      "CLI_INVALID_OUTPUT",
      "Browser CLI returned a non-object value",
      { retryable: true },
    );
  }
  return result;
}

function unwrapTurnResult(wrapper, context, expected) {
  if (
    wrapper._origin !== context.session.origin &&
    wrapper._mutation_attempted !== false &&
    expected.mutationAction
  ) {
    throw mutationFailure(
      expected.mutationAction,
      new BoundaryError("NAVIGATION_DRIFT", "Turn outcome is unknown"),
    );
  }
  verifyOrigin(wrapper, context);
  if (wrapper._boundary_error) {
    if (wrapper._mutation_attempted && expected.mutationAction) {
      throw mutationFailure(
        expected.mutationAction,
        new BoundaryError(wrapper._boundary_error, "Turn outcome is unknown"),
      );
    }
    throw browserStateError(wrapper._boundary_error);
  }
  const result = wrapper.result;
  if (
    !result ||
    result.project_id !== expected.projectId ||
    result.chat_id !== expected.chatId ||
    (expected.rootMode === "existing" &&
      result.root_frame_id !== expected.rootFrameId) ||
    !result.delivery ||
    result.delivery.authored_prompt_sha256 !== expected.authoredPromptSha256 ||
    result.delivery.delivery_text_sha256 !== expected.deliveryTextSha256 ||
    result.delivery.root_frame_id !== result.root_frame_id
  ) {
    if (expected.mutationAction) {
      throw mutationFailure(
        expected.mutationAction,
        new BoundaryError(
          "IDENTITY_MISMATCH",
          "Turn result identity or prompt proof is inconsistent",
        ),
      );
    }
    throw identityMismatch("Turn result identity or prompt proof is inconsistent");
  }
  return result;
}

function browserStateError(code) {
  const messages = {
    AMBIGUOUS_APPROVAL: "Approval state is ambiguous",
    APPROVAL_NOT_CLEARED: "Approval clearance could not be verified",
    DELIVERY_MISMATCH: "Delivered turn does not match the expected prompt",
    DELIVERY_NOT_CONFIRMED: "Prompt delivery could not be confirmed",
    IDENTITY_MISMATCH: "Browser identity is inconsistent",
    MALFORMED_BROWSER_STATE: "Browser state is malformed or ambiguous",
    NAVIGATION_DRIFT: "Browser session navigated away from the expected origin",
  };
  return new BoundaryError(
    Object.hasOwn(messages, code) ? code : "MALFORMED_BROWSER_STATE",
    messages[code] ?? "Browser state is malformed or ambiguous",
    { retryable: false },
  );
}

async function runObservation(runCommand, source, context) {
  const stdout = await runCommand(
    ["--raw", `-s=${context.session.session_id}`, "run-code", source],
    { deadlineMs: context.deadlineMs },
  );
  const result = parseCliJson(stdout);
  if (result === null || typeof result !== "object" || Array.isArray(result)) {
    throw new BoundaryError(
      "CLI_INVALID_OUTPUT",
      "Browser CLI returned a non-object value",
      { retryable: true },
    );
  }
  return result;
}

function verifyOrigin(result, context) {
  if (result._origin !== context.session.origin) {
    throw new BoundaryError(
      "NAVIGATION_DRIFT",
      "Browser session is open on an unexpected origin",
      { retryable: false },
    );
  }
}

function identityMismatch(message) {
  return new BoundaryError("IDENTITY_MISMATCH", message, {
    retryable: false,
  });
}

function mutationFailure(action, error) {
  if (error instanceof BoundaryError && error.code === "CLI_UNAVAILABLE") {
    return error;
  }
  return new BoundaryError(
    `${action}_OUTCOME_UNKNOWN`,
    `Browser session ${action.toLowerCase()} outcome is unknown`,
    {
      outcome: "unknown_outcome",
      retryable: false,
      evidence: {
        cause_code: error instanceof BoundaryError
          ? error.code
          : "BOUNDARY_FAILURE",
      },
    },
  );
}

async function inspectSession(runCommand, context, startedAt) {
  const stdout = await runCommand(
    [
      "--raw",
      `-s=${context.session.session_id}`,
      "run-code",
      SESSION_INSPECT_SOURCE,
    ],
    { deadlineMs: remainingDeadline(context.deadlineMs, startedAt) },
  );
  const result = parseCliJson(stdout);
  if (result === null || typeof result !== "object" || Array.isArray(result)) {
    // valid JSON but not an object (array / number / string): a malformed payload, not drift
    throw new BoundaryError(
      "CLI_INVALID_OUTPUT",
      "Browser CLI returned a non-object value",
      { retryable: true },
    );
  }
  if (result.origin !== context.session.origin) {
    throw new BoundaryError(
      "NAVIGATION_DRIFT",
      "Browser session is open on an unexpected origin",
      { retryable: false },
    );
  }
  return result;
}

function parseCliJson(stdout) {
  try {
    return JSON.parse(stdout);
  } catch {
    throw new BoundaryError(
      "CLI_INVALID_OUTPUT",
      "Browser CLI returned invalid JSON",
      { retryable: true },
    );
  }
}

function remainingDeadline(deadlineMs, startedAt) {
  const remaining = deadlineMs - Math.floor(performance.now() - startedAt);
  if (remaining <= 0) {
    throw new BoundaryError(
      "CLI_TIMEOUT",
      "Browser CLI exceeded the operation deadline",
      { retryable: true },
    );
  }
  return remaining;
}

async function tryDetachSession(runCommand, context, startedAt) {
  try {
    const stdout = await runCommand(
      ["--json", `-s=${context.session.session_id}`, "detach"],
      { deadlineMs: remainingDeadline(context.deadlineMs, startedAt) },
    );
    const result = parseCliJson(stdout);
    return (
      result?.session === context.session.session_id &&
      result?.status === "detached"
    );
  } catch {
    return false;
  }
}

export function createDefaultHandlers(options = {}) {
  return {
    "session.attach": createSessionAttachHandler({
      ...options,
      browserOwner: options.browserOwner ?? process.env.BROWSER_OWNER_NAME,
    }),
    "session.inspect": createSessionInspectHandler(options),
    "session.detach": createSessionDetachHandler(options),
    "project.inspect": createProjectInspectHandler(options),
    "chat.inspect": createChatInspectHandler(options),
    "agent_context.inspect": createContextInspectHandler(options),
    "turn.submit_wait": createSubmitTurnWaitHandler(options),
    "turn.wait": createWaitTurnHandler(options),
    "approval.resolve": createResolveApprovalHandler(options),
  };
}
