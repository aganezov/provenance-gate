import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { executeInput } from "../src/main.mjs";
import { BoundaryError, PROTOCOL_VERSION } from "../src/protocol.mjs";

const MAIN_PATH = fileURLToPath(new URL("../src/main.mjs", import.meta.url));

function request() {
  return {
    protocol_version: PROTOCOL_VERSION,
    request_id: "request-001",
    operation: "session.inspect",
    session: {
      session_id: "session-001",
      origin: "http://127.0.0.1:8875",
    },
    deadline_ms: 15000,
    payload: {},
  };
}

test("injected operation crosses the same dispatcher", async () => {
  const output = await executeInput(JSON.stringify(request()), {
    "session.inspect": async (_payload, context) => ({
      authenticated: true,
      origin: context.session.origin,
      profile_ready: true,
    }),
  });
  const response = JSON.parse(output);
  assert.equal(response.outcome, "completed");
  assert.equal(response.request_id, "request-001");
  assert.equal(response.result.profile_ready, true);
});

test("unimplemented operation returns structured not_started", async () => {
  const response = JSON.parse(await executeInput(JSON.stringify(request())));
  assert.equal(response.outcome, "not_started");
  assert.equal(response.error.code, "OPERATION_NOT_IMPLEMENTED");
  assert.equal(response.error.retryable, false);
});

test("handler failures are bounded structured errors", async () => {
  const output = await executeInput(JSON.stringify(request()), {
    "session.inspect": async () => {
      throw new BoundaryError("INSPECTION_FAILED", "Inspection failed");
    },
  });
  const response = JSON.parse(output);
  assert.equal(response.outcome, "not_started");
  assert.equal(response.error.code, "INSPECTION_FAILED");
});

test("unexpected handler failures do not expose internal messages", async () => {
  const output = await executeInput(JSON.stringify(request()), {
    "session.inspect": async () => {
      throw new Error("private implementation detail");
    },
  });
  const response = JSON.parse(output);
  assert.equal(response.error.code, "BOUNDARY_FAILURE");
  assert.equal(response.error.message, "Browser boundary failed");
  assert.doesNotMatch(output, /private implementation detail/);
});

test("CLI emits one JSON response and no stderr for correlated errors", () => {
  const unimplemented = {
    ...request(),
    operation: "project.inspect",
  };
  const result = spawnSync(process.execPath, [MAIN_PATH], {
    input: JSON.stringify(unimplemented),
    encoding: "utf8",
  });
  assert.equal(result.status, 0);
  assert.equal(result.stderr, "");
  const response = JSON.parse(result.stdout);
  assert.equal(response.error.code, "OPERATION_NOT_IMPLEMENTED");
  assert.equal(result.stdout.trim().split("\n").length, 1);
});

test("uncorrelatable malformed input exits nonzero with no stdout", () => {
  const result = spawnSync(process.execPath, [MAIN_PATH], {
    input: "not json",
    encoding: "utf8",
  });
  assert.equal(result.status, 2);
  assert.equal(result.stdout, "");
  assert.match(result.stderr, /valid JSON/);
});
