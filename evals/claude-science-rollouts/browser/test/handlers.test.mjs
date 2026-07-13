import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  SESSION_INSPECT_SOURCE,
  createDefaultHandlers,
  createChatInspectHandler,
  createContextInspectHandler,
  createAttachmentUploadHandler,
  createNewChatHandler,
  createModelSelectHandler,
  createProjectCreateHandler,
  createProjectInspectHandler,
  createResolveApprovalHandler,
  createSessionAttachHandler,
  createSessionDetachHandler,
  createSessionInspectHandler,
  createSubmitTurnWaitHandler,
  createWaitTurnHandler,
} from "../src/handlers.mjs";
import { BoundaryError } from "../src/protocol.mjs";
import { deliveryTextSha256, sha256Hex } from "../src/turns.mjs";

const context = {
  deadlineMs: 15000,
  requestId: "request-001",
  session: {
    origin: "http://127.0.0.1:8875",
    session_id: "session-001",
  },
};

function runCodeEnvelope(result) {
  return JSON.stringify({ result: JSON.stringify(result) });
}

test("session inspection invokes one bounded CLI read", async () => {
  let invocation;
  const handler = createSessionInspectHandler({
    async runCommand(args, options) {
      invocation = { args, options };
      return JSON.stringify({
        authenticated: true,
        origin: context.session.origin,
        profile_ready: true,
      });
    },
  });

  const result = await handler({}, context);
  assert.deepEqual(result, {
    authenticated: true,
    origin: context.session.origin,
    profile_ready: true,
  });
  assert.deepEqual(invocation, {
    args: [
      "--raw",
      "-s=session-001",
      "run-code",
      SESSION_INSPECT_SOURCE,
    ],
    options: { deadlineMs: 15000 },
  });
});

test("session attachment binds an external browser owner and verifies it", async () => {
  const invocations = [];
  const handler = createSessionAttachHandler({
    browserOwner: "existing-browser",
    async runCommand(args, options) {
      invocations.push({ args, options });
      if (args.includes("attach")) return "attached";
      return JSON.stringify({
        authenticated: true,
        origin: context.session.origin,
        profile_ready: true,
      });
    },
  });

  const result = await handler({}, context);
  assert.equal(result.authenticated, true);
  assert.deepEqual(invocations[0].args, [
    "--raw",
    "attach",
    "existing-browser",
    "--session",
    "session-001",
  ]);
  assert.deepEqual(invocations[1].args, [
    "--raw",
    "-s=session-001",
    "run-code",
    SESSION_INSPECT_SOURCE,
  ]);
  assert.ok(invocations[0].options.deadlineMs <= context.deadlineMs);
  assert.ok(invocations[1].options.deadlineMs <= context.deadlineMs);
});

test("session attachment requires an external browser owner", async () => {
  const handler = createSessionAttachHandler({
    async runCommand() {
      assert.fail("CLI must not run without an external browser owner");
    },
  });
  await assert.rejects(
    handler({}, context),
    (error) =>
      error instanceof BoundaryError && error.code === "BROWSER_OWNER_REQUIRED",
  );
});

test("session attachment rejects an invalid configured owner before CLI use", async () => {
  let invoked = false;
  const handler = createSessionAttachHandler({
    browserOwner: "invalid owner",
    async runCommand() {
      invoked = true;
    },
  });
  await assert.rejects(
    handler({}, context),
    (error) =>
      error instanceof BoundaryError && error.code === "INVALID_BROWSER_OWNER",
  );
  assert.equal(invoked, false);
});

test("session attachment treats a CLI timeout as non-retryable unknown outcome", async () => {
  const handler = createSessionAttachHandler({
    browserOwner: "existing-browser",
    async runCommand() {
      throw new BoundaryError("CLI_TIMEOUT", "timed out", { retryable: true });
    },
  });
  await assert.rejects(
    handler({}, context),
    (error) =>
      error instanceof BoundaryError &&
      error.code === "ATTACH_OUTCOME_UNKNOWN" &&
      error.outcome === "unknown_outcome" &&
      error.retryable === false,
  );
});

test("session attachment cleans up when post-attach verification fails", async () => {
  const invocations = [];
  const handler = createSessionAttachHandler({
    browserOwner: "existing-browser",
    async runCommand(args) {
      invocations.push(args);
      if (args.includes("attach")) return "attached";
      if (args.includes("detach")) {
        return JSON.stringify({ session: "session-001", status: "detached" });
      }
      return JSON.stringify({
        authenticated: true,
        origin: "http://127.0.0.1:9999",
        profile_ready: true,
      });
    },
  });

  await assert.rejects(
    handler({}, context),
    (error) => error instanceof BoundaryError && error.code === "NAVIGATION_DRIFT",
  );
  assert.deepEqual(invocations.at(-1), ["--json", "-s=session-001", "detach"]);
});

test("session attachment reports unknown outcome when cleanup is unconfirmed", async () => {
  const handler = createSessionAttachHandler({
    browserOwner: "existing-browser",
    async runCommand(args) {
      if (args.includes("attach")) return "attached";
      if (args.includes("detach")) throw new Error("cleanup failed");
      return "not JSON";
    },
  });

  await assert.rejects(
    handler({}, context),
    (error) =>
      error instanceof BoundaryError &&
      error.code === "ATTACH_CLEANUP_UNCONFIRMED" &&
      error.outcome === "unknown_outcome" &&
      error.retryable === false &&
      error.evidence.cause_code === "CLI_INVALID_OUTPUT",
  );
});

test("session detachment invokes one bounded CLI operation", async () => {
  let invocation;
  const handler = createSessionDetachHandler({
    async runCommand(args, options) {
      invocation = { args, options };
      return JSON.stringify({ session: "session-001", status: "detached" });
    },
  });

  assert.deepEqual(await handler({}, context), { detached: true });
  assert.deepEqual(invocation, {
    args: ["--json", "-s=session-001", "detach"],
    options: { deadlineMs: 15000 },
  });
});

test("session detachment is unknown when the CLI cannot confirm it", async () => {
  const handler = createSessionDetachHandler({
    async runCommand() {
      return JSON.stringify({ session: "other-session", status: "detached" });
    },
  });
  await assert.rejects(
    handler({}, context),
    (error) =>
      error instanceof BoundaryError &&
      error.code === "DETACH_OUTCOME_UNKNOWN" &&
      error.outcome === "unknown_outcome" &&
      error.retryable === false,
  );
});

test("session detachment is unknown when CLI output is malformed", async () => {
  const handler = createSessionDetachHandler({
    async runCommand() {
      return "not JSON";
    },
  });
  await assert.rejects(
    handler({}, context),
    (error) =>
      error instanceof BoundaryError &&
      error.code === "DETACH_OUTCOME_UNKNOWN" &&
      error.evidence.cause_code === "CLI_INVALID_OUTPUT",
  );
});

test("session detachment treats a CLI timeout as non-retryable unknown outcome", async () => {
  const handler = createSessionDetachHandler({
    async runCommand() {
      throw new BoundaryError("CLI_TIMEOUT", "timed out", { retryable: true });
    },
  });
  await assert.rejects(
    handler({}, context),
    (error) =>
      error instanceof BoundaryError &&
      error.code === "DETACH_OUTCOME_UNKNOWN" &&
      error.outcome === "unknown_outcome" &&
      error.retryable === false,
  );
});

test("session inspection fails closed on origin drift", async () => {
  const handler = createSessionInspectHandler({
    async runCommand() {
      return JSON.stringify({
        authenticated: true,
        origin: "http://127.0.0.1:9999",
        profile_ready: true,
      });
    },
  });
  await assert.rejects(
    handler({}, context),
    (error) => error instanceof BoundaryError && error.code === "NAVIGATION_DRIFT",
  );
});

test("session inspection rejects malformed CLI output", async () => {
  const handler = createSessionInspectHandler({
    async runCommand() {
      return "not JSON";
    },
  });
  await assert.rejects(
    handler({}, context),
    (error) => error instanceof BoundaryError && error.code === "CLI_INVALID_OUTPUT",
  );
});

test("session inspection rejects valid-JSON non-object output as malformed, not drift", async () => {
  for (const payload of ["[1,2,3]", "42", "\"origin\""]) {
    const handler = createSessionInspectHandler({
      async runCommand() {
        return payload;
      },
    });
    await assert.rejects(
      handler({}, context),
      (error) => error instanceof BoundaryError && error.code === "CLI_INVALID_OUTPUT",
    );
  }
});

test("default handlers attach an owner from BROWSER_OWNER_NAME", async () => {
  const previous = process.env.BROWSER_OWNER_NAME;
  process.env.BROWSER_OWNER_NAME = "env-browser";
  try {
    const invocations = [];
    const handlers = createDefaultHandlers({
      async runCommand(args) {
        invocations.push(args);
        if (args.includes("attach")) return "attached";
        return JSON.stringify({
          authenticated: true,
          origin: context.session.origin,
          profile_ready: true,
        });
      },
    });
    const result = await handlers["session.attach"]({}, context);
    assert.equal(result.authenticated, true);
    assert.deepEqual(invocations[0], [
      "--raw",
      "attach",
      "env-browser",
      "--session",
      "session-001",
    ]);
  } finally {
    if (previous === undefined) delete process.env.BROWSER_OWNER_NAME;
    else process.env.BROWSER_OWNER_NAME = previous;
  }
});

test("project inspection returns a verified rooted observation", async () => {
  let invocation;
  const handler = createProjectInspectHandler({
    async runCommand(args, options) {
      invocation = { args, options };
      return runCodeEnvelope({
        _origin: context.session.origin,
        project_id: "project-001",
        verified: true,
        composer_empty: true,
        user_turn_count: 1,
        root_frame_id: "root-001",
        root_state: "completed",
        _root_project_id: "project-001",
      });
    },
  });
  assert.deepEqual(
    await handler({ project_id: "project-001" }, context),
    {
      project_id: "project-001",
      verified: true,
      composer_empty: true,
      user_turn_count: 1,
      root_frame_id: "root-001",
      root_state: "completed",
    },
  );
  assert.deepEqual(invocation.options, { deadlineMs: 15000 });
  assert.deepEqual(invocation.args.slice(0, 3), [
    "--json",
    "-s=session-001",
    "run-code",
  ]);
});

test("chat inspection returns bounded transcript and identities", async () => {
  const handler = createChatInspectHandler({
    async runCommand() {
      return runCodeEnvelope({
        _origin: context.session.origin,
        project_id: "project-001",
        chat_id: "chat-001",
        transcript: [
          {
            turn_id: "turn-user",
            role: "user",
            text: "Question",
            truncated: false,
          },
          {
            turn_id: "turn-assistant",
            role: "assistant",
            text: "Answer",
            truncated: false,
          },
        ],
        user_turn_count: 1,
        composer_empty: true,
        root_frame_id: "root-001",
        response_control_id: "turn-assistant",
        current_turn_state: "indeterminate",
        approval_cards: [],
        _root_project_id: "project-001",
        _expected: {
          project_id: "project-001",
          chat_id: "chat-001",
          root_frame_id: "root-001",
        },
      });
    },
  });
  const result = await handler(
    {
      project_id: "project-001",
      chat_id: "chat-001",
      root_frame_id: "root-001",
    },
    context,
  );
  assert.equal(result.project_id, "project-001");
  assert.equal(result.chat_id, "chat-001");
  assert.equal(result.transcript.length, 2);
  assert.equal(result.transcript[0].turn_id, "turn-user");
});

test("context inspection returns only skills and context hash", async () => {
  const handler = createContextInspectHandler({
    async runCommand() {
      return runCodeEnvelope({
        _origin: context.session.origin,
        project_id: "project-001",
        enabled_skills: ["Audit"],
        context_hash: "a".repeat(64),
      });
    },
  });
  assert.deepEqual(
    await handler({ project_id: "project-001" }, context),
    {
      project_id: "project-001",
      enabled_skills: ["Audit"],
      context_hash: "a".repeat(64),
    },
  );
});

test("G3a observation handlers reject origin and identity drift", async () => {
  const navigation = createProjectInspectHandler({
    async runCommand() {
      return runCodeEnvelope({
        _origin: "http://127.0.0.1:9999",
        project_id: "project-001",
      });
    },
  });
  await assert.rejects(
    navigation({ project_id: "project-001" }, context),
    (error) => error instanceof BoundaryError && error.code === "NAVIGATION_DRIFT",
  );

  const identity = createChatInspectHandler({
    async runCommand() {
      return runCodeEnvelope({
        _origin: context.session.origin,
        project_id: "project-other",
        chat_id: "chat-001",
      });
    },
  });
  await assert.rejects(
    identity({ project_id: "project-001", chat_id: "chat-001" }, context),
    (error) => error instanceof BoundaryError && error.code === "IDENTITY_MISMATCH",
  );

  const unexpectedRoot = createChatInspectHandler({
    async runCommand() {
      return runCodeEnvelope({
        _origin: context.session.origin,
        project_id: "project-001",
        chat_id: "chat-001",
        root_frame_id: "root-001",
        _root_project_id: "project-001",
      });
    },
  });
  await assert.rejects(
    unexpectedRoot({ project_id: "project-001", chat_id: "chat-001" }, context),
    (error) => error instanceof BoundaryError && error.code === "IDENTITY_MISMATCH",
  );

  const wrongRootProject = createChatInspectHandler({
    async runCommand() {
      return runCodeEnvelope({
        _origin: context.session.origin,
        project_id: "project-001",
        chat_id: "chat-001",
        root_frame_id: "root-001",
        _root_project_id: "project-other",
      });
    },
  });
  await assert.rejects(
    wrongRootProject(
      {
        project_id: "project-001",
        chat_id: "chat-001",
        root_frame_id: "root-001",
      },
      context,
    ),
    (error) => error instanceof BoundaryError && error.code === "IDENTITY_MISMATCH",
  );
});

test("G3a observation handlers reject malformed and non-object CLI output", async () => {
  for (const payload of ["not JSON", "[]", "42"]) {
    const handler = createProjectInspectHandler({
      async runCommand() {
        return payload;
      },
    });
    await assert.rejects(
      handler({ project_id: "project-001" }, context),
      (error) => error instanceof BoundaryError && error.code === "CLI_INVALID_OUTPUT",
    );
  }
});

test("project creation returns only a verified fresh project", async () => {
  let source;
  const handler = createProjectCreateHandler({
    async runCommand(args) {
      source = args[3];
      return runCodeEnvelope({
        _origin: context.session.origin,
        _mutation_attempted: true,
        result: {
          project_id: "project-created",
          verified: true,
          composer_empty: true,
          user_turn_count: 0,
          root_frame_id: null,
          root_state: null,
        },
      });
    },
  });
  const result = await handler({ name: "PBMC bare replicate" }, context);
  assert.equal(result.project_id, "project-created");
  assert.equal(result.user_turn_count, 0);
  assert.match(source, /New project/);
  assert.equal(source.match(/await create\.click\(\)/g)?.length, 1);
});

test("new chat returns a verified rootless blank chat", async () => {
  const handler = createNewChatHandler({
    async runCommand() {
      return runCodeEnvelope({
        _origin: context.session.origin,
        _mutation_attempted: false,
        result: {
          project_id: "project-created",
          chat_id: "chat-created",
          transcript: [],
          user_turn_count: 0,
          composer_empty: true,
          root_frame_id: null,
          response_control_id: null,
          current_turn_state: "indeterminate",
          approval_cards: [],
        },
      });
    },
  });
  const result = await handler({ project_id: "project-created" }, context);
  assert.equal(result.chat_id, "chat-created");
  assert.equal(result.root_frame_id, null);
});

test("model selection confirms an exact label on the blank chat", async () => {
  let source;
  const handler = createModelSelectHandler({
    async runCommand(args) {
      source = args[3];
      return runCodeEnvelope({
        _origin: context.session.origin,
        _mutation_attempted: true,
        result: {
          project_id: "project-created",
          chat_id: "chat-created",
          model_label: "Research Fast",
          previous_model_label: "Research Default",
          changed: true,
          confirmed: true,
        },
      });
    },
  });

  const result = await handler(
    {
      project_id: "project-created",
      chat_id: "chat-created",
      model_label: "Research Fast",
    },
    context,
  );

  assert.equal(result.confirmed, true);
  assert.equal(result.changed, true);
  assert.match(source, /MODEL_SELECTION_REQUIRES_BLANK_CHAT/);
});

test("post-click model ambiguity is a non-retryable unknown outcome", async () => {
  const handler = createModelSelectHandler({
    async runCommand() {
      return runCodeEnvelope({
        _origin: context.session.origin,
        _mutation_attempted: true,
        _boundary_error: "MODEL_SELECTION_UNCONFIRMED",
      });
    },
  });

  await assert.rejects(
    handler(
      {
        project_id: "project-created",
        chat_id: "chat-created",
        model_label: "Research Fast",
      },
      context,
    ),
    (error) =>
      error instanceof BoundaryError &&
      error.code === "SELECT_MODEL_OUTCOME_UNKNOWN" &&
      error.outcome === "unknown_outcome" &&
      error.retryable === false,
  );
});

test("attachment upload sets the declared path in one browser callback", async () => {
  const directory = mkdtempSync(join(tmpdir(), "browser-g3c-"));
  const sourcePath = join(directory, "pbmc_tiny_seed.csv");
  writeFileSync(sourcePath, "cell_id,value\nC001,1\n");
  const invocations = [];
  let runCodeCalls = 0;
  const handler = createAttachmentUploadHandler({
    async runCommand(args) {
      invocations.push(args);
      runCodeCalls += 1;
      if (runCodeCalls === 1) {
        return runCodeEnvelope({
          _origin: context.session.origin,
          _mutation_attempted: true,
          result: { ready: true, chat_id: "chat-current" },
        });
      }
      return runCodeEnvelope({
        _origin: context.session.origin,
        result: {
          project_id: "project-created",
          chat_id: "chat-current",
          filename: "pbmc_tiny_seed.csv",
          accepted: true,
        },
      });
    },
  });
  try {
    const result = await handler({
      project_id: "project-created",
      chat_id: "chat-created",
      source_path: sourcePath,
    }, context);
    assert.deepEqual(result, {
      project_id: "project-created",
      chat_id: "chat-current",
      filename: "pbmc_tiny_seed.csv",
      accepted: true,
    });
    assert.equal(invocations.length, 2);
    assert.match(invocations[0][3], new RegExp(sourcePath));
    assert.doesNotMatch(invocations[1][3], new RegExp(sourcePath));
    assert.doesNotMatch(JSON.stringify(result), new RegExp(sourcePath));
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("attachment upload rejects an unavailable source before browser use", async () => {
  let invoked = false;
  const handler = createAttachmentUploadHandler({
    async runCommand() {
      invoked = true;
    },
  });
  await assert.rejects(
    handler({
      project_id: "project-created",
      chat_id: "chat-created",
      source_path: "/private/tmp/missing-pbmc-seed.csv",
    }, context),
    (error) =>
      error instanceof BoundaryError &&
      error.code === "ATTACHMENT_SOURCE_INVALID" &&
      !error.message.includes("missing-pbmc-seed"),
  );
  assert.equal(invoked, false);
});

test("attachment chooser phase is preserved as bounded pre-upload evidence", async () => {
  const directory = mkdtempSync(join(tmpdir(), "browser-g3c-chooser-"));
  const sourcePath = join(directory, "pbmc_tiny_seed.csv");
  writeFileSync(sourcePath, "cell_id,value\nC001,1\n");
  const invocations = [];
  const handler = createAttachmentUploadHandler({
    async runCommand(args) {
      invocations.push(args);
      return runCodeEnvelope({
        _origin: context.session.origin,
        _mutation_attempted: false,
        _boundary_error: "ATTACHMENT_INPUT_UNAVAILABLE",
      });
    },
  });
  try {
    await assert.rejects(
      handler({
        project_id: "project-created",
        chat_id: "chat-provisional",
        source_path: sourcePath,
      }, context),
      (error) =>
        error instanceof BoundaryError &&
        error.code === "MALFORMED_BROWSER_STATE" &&
        error.outcome === "not_started" &&
        error.retryable === false &&
        error.evidence.cause_code === "ATTACHMENT_INPUT_UNAVAILABLE",
    );
    assert.equal(invocations.length, 1);
    assert.equal(invocations.some((args) => args[2] === "upload"), false);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("attachment callback failure is unknown because selection may have occurred", async () => {
  const directory = mkdtempSync(join(tmpdir(), "browser-g3c-cli-"));
  const sourcePath = join(directory, "pbmc_tiny_seed.csv");
  writeFileSync(sourcePath, "cell_id,value\nC001,1\n");
  const invocations = [];
  const handler = createAttachmentUploadHandler({
    async runCommand(args) {
      invocations.push(args);
      throw new BoundaryError("CLI_INVALID_OUTPUT", "invalid structured result");
    },
  });
  try {
    await assert.rejects(
      handler({
        project_id: "project-created",
        chat_id: "chat-provisional",
        source_path: sourcePath,
      }, context),
      (error) =>
        error instanceof BoundaryError &&
        error.code === "UPLOAD_OUTCOME_UNKNOWN" &&
        error.outcome === "unknown_outcome" &&
        error.retryable === false &&
        error.evidence.cause_code === "CLI_INVALID_OUTPUT",
    );
    assert.equal(invocations.length, 1);
    assert.equal(invocations.some((args) => args[2] === "upload"), false);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("attachment callback timeout is unknown and never replayed", async () => {
  const directory = mkdtempSync(join(tmpdir(), "browser-g3c-timeout-"));
  const sourcePath = join(directory, "pbmc_tiny_seed.csv");
  writeFileSync(sourcePath, "cell_id,value\nC001,1\n");
  const invocations = [];
  const handler = createAttachmentUploadHandler({
    async runCommand(args) {
      invocations.push(args);
      throw new BoundaryError("CLI_TIMEOUT", "browser command timed out", {
        outcome: "unknown_outcome",
        retryable: false,
      });
    },
  });
  try {
    await assert.rejects(
      handler({
        project_id: "project-created",
        chat_id: "chat-provisional",
        source_path: sourcePath,
      }, context),
      (error) =>
        error instanceof BoundaryError &&
        error.code === "UPLOAD_OUTCOME_UNKNOWN" &&
        error.outcome === "unknown_outcome" &&
        error.retryable === false &&
        error.evidence.cause_code === "CLI_TIMEOUT",
    );
    assert.equal(invocations.length, 1);
    assert.equal(invocations.some((args) => args[2] === "upload"), false);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("attachment verification failure after upload is unknown and never replayed", async () => {
  const directory = mkdtempSync(join(tmpdir(), "browser-g3c-unknown-"));
  const sourcePath = join(directory, "pbmc_tiny_seed.csv");
  writeFileSync(sourcePath, "cell_id,value\nC001,1\n");
  const invocations = [];
  let runCodeCalls = 0;
  const handler = createAttachmentUploadHandler({
    async runCommand(args) {
      invocations.push(args);
      runCodeCalls += 1;
      return runCodeEnvelope(
        runCodeCalls === 1
          ? {
              _origin: context.session.origin,
              _mutation_attempted: true,
              result: { ready: true, chat_id: "chat-created" },
            }
          : {
              _origin: context.session.origin,
              _boundary_error: "ATTACHMENT_NOT_VERIFIED",
            },
      );
    },
  });
  try {
    await assert.rejects(
      handler({
        project_id: "project-created",
        chat_id: "chat-created",
        source_path: sourcePath,
      }, context),
      (error) =>
        error instanceof BoundaryError &&
        error.code === "UPLOAD_OUTCOME_UNKNOWN" &&
        error.outcome === "unknown_outcome" &&
        error.retryable === false,
    );
    assert.equal(invocations.length, 2);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

function turnResult({ state = "settled", continuation = null } = {}) {
  const delivery = {
    root_frame_id: "root-001",
    authored_prompt_sha256: sha256Hex("Do one thing"),
    delivery_text_sha256: deliveryTextSha256("Do one thing"),
    normalized_user_turn_id: "turn-user-new",
  };
  return {
    project_id: "project-001",
    chat_id: "chat-001",
    root_frame_id: "root-001",
    turn_state: state,
    root_created: false,
    delivery,
    settled: state === "settled"
      ? {
          stop_hidden: true,
          stable_samples: 2,
          new_response_control_id: "turn-assistant-new",
        }
      : null,
    approval: null,
    continuation,
  };
}

test("submit verifies raw hash before one bounded browser invocation", async () => {
  let invocations = 0;
  const handler = createSubmitTurnWaitHandler({
    async runCommand(args) {
      invocations += 1;
      assert.equal(args[2], "run-code");
      return runCodeEnvelope({
        _origin: context.session.origin,
        _mutation_attempted: true,
        result: turnResult(),
      });
    },
  });
  const payload = {
    project_id: "project-001",
    chat_id: "chat-001",
    root_mode: "existing",
    root_frame_id: "root-001",
    prompt: "Do one thing",
    authored_prompt_sha256: sha256Hex("Do one thing"),
  };
  assert.equal((await handler(payload, context)).turn_state, "settled");
  assert.equal(invocations, 1);
  await assert.rejects(
    handler({ ...payload, authored_prompt_sha256: "a".repeat(64) }, context),
    (error) =>
      error instanceof BoundaryError && error.code === "PROMPT_HASH_MISMATCH",
  );
  assert.equal(invocations, 1);
});

test("browser code uses the structured CLI envelope", async () => {
  const handler = createSubmitTurnWaitHandler({
    async runCommand(args) {
      assert.equal(args[0], "--json");
      return JSON.stringify({
        result: JSON.stringify({
          _origin: context.session.origin,
          _mutation_attempted: true,
          result: turnResult(),
        }),
        snapshot: { file: "external-snapshot.yml" },
      });
    },
  });
  const result = await handler(
    {
      project_id: "project-001",
      chat_id: "chat-001",
      root_mode: "existing",
      root_frame_id: "root-001",
      prompt: "Do one thing",
      authored_prompt_sha256: sha256Hex("Do one thing"),
    },
    context,
  );
  assert.equal(result.turn_state, "settled");
});

test("browser code rejects malformed structured CLI envelopes", async () => {
  const payloads = [
    [
      JSON.stringify({ _origin: context.session.origin }),
      "CLI_INVALID_OUTPUT",
    ],
    [JSON.stringify({ isError: true, error: "internal failure" }), "CLI_COMMAND_FAILED"],
    [JSON.stringify({}), "CLI_INVALID_OUTPUT"],
    [JSON.stringify({ result: {} }), "CLI_INVALID_OUTPUT"],
  ];
  for (const [stdout, expectedCode] of payloads) {
    const handler = createProjectInspectHandler({
      async runCommand() {
        return stdout;
      },
    });
    await assert.rejects(
      handler({ project_id: "project-001" }, context),
      (error) => error instanceof BoundaryError && error.code === expectedCode,
    );
  }
});

test("delivered unsettled submit returns a continuation without replay", async () => {
  const continuation = {
    project_id: "project-001",
    chat_id: "chat-001",
    root_frame_id: "root-001",
    authored_prompt_sha256: sha256Hex("Do one thing"),
    delivery_text_sha256: deliveryTextSha256("Do one thing"),
    normalized_user_turn_id: "turn-user-new",
    baseline_response_control_id: "turn-assistant-old",
  };
  const handler = createSubmitTurnWaitHandler({
    async runCommand() {
      return runCodeEnvelope({
        _origin: context.session.origin,
        _mutation_attempted: true,
        result: turnResult({ state: "busy", continuation }),
      });
    },
  });
  const result = await handler(
    {
      project_id: "project-001",
      chat_id: "chat-001",
      root_mode: "existing",
      root_frame_id: "root-001",
      prompt: "Do one thing",
      authored_prompt_sha256: sha256Hex("Do one thing"),
    },
    context,
  );
  assert.deepEqual(result.continuation, continuation);
});

test("wait consumes hashes and never serializes a submit action", async () => {
  const continuation = {
    project_id: "project-001",
    chat_id: "chat-001",
    root_frame_id: "root-001",
    authored_prompt_sha256: sha256Hex("Do one thing"),
    delivery_text_sha256: deliveryTextSha256("Do one thing"),
    normalized_user_turn_id: "turn-user-new",
    baseline_response_control_id: "turn-assistant-old",
  };
  let source;
  const handler = createWaitTurnHandler({
    async runCommand(args) {
      source = args[3];
      return runCodeEnvelope({
        _origin: context.session.origin,
        result: turnResult(),
      });
    },
  });
  const result = await handler(
    { project_id: "project-001", chat_id: "chat-001", continuation },
    context,
  );
  assert.equal(result.delivery.authored_prompt_sha256, continuation.authored_prompt_sha256);
  assert.equal(result.delivery.delivery_text_sha256, continuation.delivery_text_sha256);
  assert.doesNotMatch(source, /insertText|await send\.click/);
});

test("ambiguous post-submit state becomes a non-retryable unknown outcome", async () => {
  const handler = createSubmitTurnWaitHandler({
    async runCommand() {
      return runCodeEnvelope({
        _origin: context.session.origin,
        _mutation_attempted: true,
        _boundary_error: "AMBIGUOUS_APPROVAL",
      });
    },
  });
  await assert.rejects(
    handler(
      {
        project_id: "project-001",
        chat_id: "chat-001",
        root_mode: "new",
        prompt: "Do one thing",
        authored_prompt_sha256: sha256Hex("Do one thing"),
      },
      context,
    ),
    (error) =>
      error instanceof BoundaryError &&
      error.code === "SUBMIT_OUTCOME_UNKNOWN" &&
      error.outcome === "unknown_outcome" &&
      error.retryable === false,
  );
});

test("approval resolution requires exact identity and verified clearance", async () => {
  const handler = createResolveApprovalHandler({
    async runCommand() {
      return runCodeEnvelope({
        _origin: context.session.origin,
        _mutation_attempted: true,
        result: {
          project_id: "project-001",
          chat_id: "chat-001",
          root_frame_id: "root-001",
          card_id: "approval:abc:0",
          decision: "allow_for_conversation",
          verified_cleared: true,
        },
      });
    },
  });
  const result = await handler(
    {
      project_id: "project-001",
      chat_id: "chat-001",
      root_frame_id: "root-001",
      card_id: "approval:abc:0",
      decision: "allow_for_conversation",
      expected_fingerprint: "c".repeat(64),
    },
    context,
  );
  assert.equal(result.verified_cleared, true);
});

test("approval ambiguity stops before mutation and post-click uncertainty is unknown", async () => {
  const payload = {
    project_id: "project-001",
    chat_id: "chat-001",
    root_frame_id: "root-001",
    card_id: "approval:abc:0",
    decision: "allow_for_conversation",
    expected_fingerprint: "c".repeat(64),
  };
  for (const [attempted, expectedCode, expectedOutcome] of [
    [false, "AMBIGUOUS_APPROVAL", "not_started"],
    [true, "APPROVAL_OUTCOME_UNKNOWN", "unknown_outcome"],
  ]) {
    const handler = createResolveApprovalHandler({
      async runCommand() {
        return runCodeEnvelope({
          _origin: context.session.origin,
          _mutation_attempted: attempted,
          _boundary_error: "AMBIGUOUS_APPROVAL",
        });
      },
    });
    await assert.rejects(
      handler(payload, context),
      (error) =>
        error instanceof BoundaryError &&
        error.code === expectedCode &&
        error.outcome === expectedOutcome,
    );
  }
});
