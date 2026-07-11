import { BoundaryError } from "./protocol.mjs";
import { runCli } from "./cli_runner.mjs";

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

export function createSessionInspectHandler({
  browserOwner,
  runCommand = runCli,
} = {}) {
  return async function inspectSession(_payload, context) {
    const startedAt = performance.now();
    if (browserOwner !== undefined) {
      if (!SESSION_NAME.test(browserOwner)) {
        throw new BoundaryError(
          "INVALID_BROWSER_OWNER",
          "Configured browser owner is invalid",
        );
      }
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
    }
    const stdout = await runCommand(
      [
        "--raw",
        `-s=${context.session.session_id}`,
        "run-code",
        SESSION_INSPECT_SOURCE,
      ],
      { deadlineMs: remainingDeadline(context.deadlineMs, startedAt) },
    );
    let result;
    try {
      result = JSON.parse(stdout);
    } catch {
      throw new BoundaryError(
        "CLI_INVALID_OUTPUT",
        "Browser CLI returned invalid JSON",
        { retryable: true },
      );
    }
    if (result?.origin !== context.session.origin) {
      throw new BoundaryError(
        "NAVIGATION_DRIFT",
        "Browser session is open on an unexpected origin",
        { retryable: false },
      );
    }
    return result;
  };
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

export function createDefaultHandlers(options = {}) {
  return {
    "session.inspect": createSessionInspectHandler({
      ...options,
      browserOwner: options.browserOwner ?? process.env.BROWSER_OWNER_NAME,
    }),
  };
}
