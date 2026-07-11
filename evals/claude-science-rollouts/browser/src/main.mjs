#!/usr/bin/env node

import { fileURLToPath } from "node:url";

import {
  BoundaryError,
  MAX_REQUEST_BYTES,
  completedResponse,
  errorResponse,
  parseRequestText,
  serializeResponse,
} from "./protocol.mjs";

export async function dispatchRequest(request, handlers = {}) {
  const startedAt = performance.now();
  const handler = handlers[request.operation];
  if (typeof handler !== "function") {
    return errorResponse(
      request,
      new BoundaryError(
        "OPERATION_NOT_IMPLEMENTED",
        "Operation is not implemented by this boundary",
      ),
      elapsed(startedAt),
    );
  }
  try {
    const result = await handler(request.payload, {
      session: request.session,
      deadlineMs: request.deadline_ms,
      requestId: request.request_id,
    });
    return completedResponse(request, result, elapsed(startedAt));
  } catch (error) {
    return errorResponse(request, error, elapsed(startedAt));
  }
}

export async function executeInput(text, handlers = {}) {
  const request = parseRequestText(text);
  return serializeResponse(await dispatchRequest(request, handlers));
}

export async function readBoundedStdin(input = process.stdin) {
  const chunks = [];
  let size = 0;
  for await (const chunk of input) {
    size += chunk.length;
    if (size > MAX_REQUEST_BYTES) {
      throw new BoundaryError(
        "REQUEST_TOO_LARGE",
        `Request exceeds the ${MAX_REQUEST_BYTES}-byte limit`,
      );
    }
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

export async function runMain({ handlers = {}, stdin = process.stdin } = {}) {
  const text = await readBoundedStdin(stdin);
  return executeInput(text, handlers);
}

function elapsed(startedAt) {
  return Math.max(0, Math.floor(performance.now() - startedAt));
}

function isMain() {
  return process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1];
}

if (isMain()) {
  try {
    process.stdout.write(await runMain());
  } catch (error) {
    const message = error instanceof Error ? error.message : "Boundary input failed";
    process.stderr.write(`${message.slice(0, 4096)}\n`);
    process.exitCode = 2;
  }
}
