import assert from "node:assert/strict";
import test from "node:test";

import {
  SESSION_INSPECT_SOURCE,
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

test("session inspection can attach an externally configured browser owner", async () => {
  const invocations = [];
  const handler = createSessionInspectHandler({
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

test("session inspection rejects an invalid configured owner before CLI use", async () => {
  let invoked = false;
  const handler = createSessionInspectHandler({
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
