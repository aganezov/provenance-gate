#!/usr/bin/env node

import { fileURLToPath } from "node:url";

import { runMain } from "../../src/main.mjs";

const handlers = {
  "session.attach": async (_payload, context) => ({
    authenticated: true,
    origin: context.session.origin,
    profile_ready: true,
  }),
  "session.inspect": async (_payload, context) => ({
    authenticated: true,
    origin: context.session.origin,
    profile_ready: true,
  }),
  "session.detach": async () => ({ detached: true }),
};

const isMain = process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1];
if (isMain) {
  try {
    process.stdout.write(await runMain({ handlers }));
  } catch (error) {
    const message = error instanceof Error ? error.message : "Mock boundary failed";
    process.stderr.write(`${message.slice(0, 4096)}\n`);
    process.exitCode = 2;
  }
}
