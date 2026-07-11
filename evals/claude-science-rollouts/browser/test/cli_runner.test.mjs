import assert from "node:assert/strict";
import test from "node:test";

import { runCli } from "../src/cli_runner.mjs";
import { BoundaryError } from "../src/protocol.mjs";

test("CLI runner uses argument arrays, bounds, and deadline", async () => {
  let invocation;
  const stdout = await runCli(["--raw", "list"], {
    cliPath: "/test/browser-cli",
    deadlineMs: 1234,
    async execFileImpl(command, args, options) {
      invocation = { command, args, options };
      return { stdout: "{}", stderr: "" };
    },
  });
  assert.equal(stdout, "{}");
  assert.equal(invocation.command, "/test/browser-cli");
  assert.deepEqual(invocation.args, ["--raw", "list"]);
  assert.equal(invocation.options.timeout, 1234);
  assert.equal(invocation.options.encoding, "utf8");
  assert.ok(invocation.options.maxBuffer > 0);
});

test("CLI runner sanitizes command failures", async () => {
  await assert.rejects(
    runCli(["run-code", "ignored"], {
      cliPath: "/test/browser-cli",
      deadlineMs: 1234,
      async execFileImpl() {
        const error = new Error("private command output");
        error.stderr = "private command output";
        throw error;
      },
    }),
    (error) =>
      error instanceof BoundaryError &&
      error.code === "CLI_COMMAND_FAILED" &&
      !error.message.includes("private"),
  );
});

test("CLI runner classifies timeouts without replay", async () => {
  await assert.rejects(
    runCli(["run-code", "ignored"], {
      cliPath: "/test/browser-cli",
      deadlineMs: 1,
      async execFileImpl() {
        const error = new Error("timed out");
        error.killed = true;
        throw error;
      },
    }),
    (error) =>
      error instanceof BoundaryError &&
      error.code === "CLI_TIMEOUT" &&
      error.retryable,
  );
});
