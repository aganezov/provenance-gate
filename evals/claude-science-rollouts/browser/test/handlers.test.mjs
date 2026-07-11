import assert from "node:assert/strict";
import test from "node:test";

import {
  SESSION_INSPECT_SOURCE,
  createDefaultHandlers,
  createSessionAttachHandler,
  createSessionDetachHandler,
  createSessionInspectHandler,
} from "../src/handlers.mjs";
import { BoundaryError } from "../src/protocol.mjs";

const context = {
  deadlineMs: 15000,
  requestId: "request-001",
  session: {
    origin: "http://127.0.0.1:8875",
    session_id: "session-001",
  },
};

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
