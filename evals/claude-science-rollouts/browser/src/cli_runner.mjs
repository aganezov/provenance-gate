import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { BoundaryError, MAX_RESPONSE_BYTES } from "./protocol.mjs";

const execFileAsync = promisify(execFile);
const DEFAULT_CLI_PATH = fileURLToPath(
  new URL("../node_modules/.bin/playwright-cli", import.meta.url),
);

export async function runCli(
  args,
  {
    cliPath = DEFAULT_CLI_PATH,
    deadlineMs,
    execFileImpl = execFileAsync,
  } = {},
) {
  try {
    const result = await execFileImpl(cliPath, args, {
      encoding: "utf8",
      maxBuffer: MAX_RESPONSE_BYTES,
      timeout: deadlineMs,
    });
    if (result.stderr) {
      throw new BoundaryError(
        "CLI_DIAGNOSTIC_OUTPUT",
        "Browser CLI emitted unexpected diagnostics",
        { retryable: true },
      );
    }
    return result.stdout;
  } catch (error) {
    if (error instanceof BoundaryError) throw error;
    if (error?.code === "ENOENT") {
      throw new BoundaryError(
        "CLI_UNAVAILABLE",
        "Browser CLI is unavailable",
      );
    }
    if (error?.killed || error?.code === "ETIMEDOUT") {
      throw new BoundaryError(
        "CLI_TIMEOUT",
        "Browser CLI exceeded the operation deadline",
        { retryable: true },
      );
    }
    throw new BoundaryError(
      "CLI_COMMAND_FAILED",
      "Browser CLI command failed",
      { retryable: true },
    );
  }
}
