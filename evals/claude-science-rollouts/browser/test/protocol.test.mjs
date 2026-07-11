import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  BoundaryError,
  OPERATIONS,
  PROTOCOL_VERSION,
  completedResponse,
  errorResponse,
  parseRequestText,
  serializeResponse,
} from "../src/protocol.mjs";

function request(overrides = {}) {
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
    ...overrides,
  };
}

test("canonical operation set is loaded from protocol.json", () => {
  const spec = JSON.parse(
    readFileSync(new URL("../protocol.json", import.meta.url), "utf8"),
  );
  assert.equal(spec.protocol_version, PROTOCOL_VERSION);
  assert.deepEqual([...OPERATIONS], spec.operations);
});

test("valid request and completed response round-trip", () => {
  const parsed = parseRequestText(JSON.stringify(request()));
  const response = completedResponse(
    parsed,
    { authenticated: true, origin: parsed.session.origin, profile_ready: true },
    12,
  );
  assert.deepEqual(JSON.parse(serializeResponse(response)), response);
});

test("credential-like fields are forbidden recursively", () => {
  assert.throws(
    () => parseRequestText(JSON.stringify(request({ payload: { password: "x" } }))),
    (error) =>
      error instanceof BoundaryError && error.code === "CREDENTIALS_FORBIDDEN",
  );
});

test("unknown and missing fields fail closed", () => {
  assert.throws(
    () => parseRequestText(JSON.stringify({ ...request(), extra: true })),
    (error) => error instanceof BoundaryError && error.code === "INVALID_FIELDS",
  );
  const missing = request();
  delete missing.deadline_ms;
  assert.throws(
    () => parseRequestText(JSON.stringify(missing)),
    (error) => error instanceof BoundaryError && error.code === "INVALID_FIELDS",
  );
});

test("origins are bare, credential-free HTTP origins", () => {
  for (const origin of [
    "file:///tmp/page",
    "http://user:pass@127.0.0.1:8875",
    "http://127.0.0.1:8875/projects/example",
    "http://127.0.0.1:8875/",
  ]) {
    assert.throws(
      () => parseRequestText(JSON.stringify(request({
        session: { ...request().session, origin },
      }))),
      (error) => error instanceof BoundaryError && error.code === "INVALID_ORIGIN",
    );
  }
});

test("unknown outcomes cannot be retryable", () => {
  const parsed = parseRequestText(JSON.stringify(request()));
  assert.throws(
    () => errorResponse(
      parsed,
      new BoundaryError("UNCERTAIN", "Operation may have started", {
        outcome: "unknown_outcome",
        retryable: true,
      }),
      10,
    ),
    (error) => error instanceof BoundaryError && error.code === "INVALID_RESPONSE",
  );
});
