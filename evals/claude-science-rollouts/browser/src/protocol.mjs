import { readFileSync } from "node:fs";

const spec = JSON.parse(
  readFileSync(new URL("../protocol.json", import.meta.url), "utf8"),
);

export const PROTOCOL_VERSION = spec.protocol_version;
export const MAX_REQUEST_BYTES = spec.limits.request_bytes;
export const MAX_RESPONSE_BYTES = spec.limits.response_bytes;
export const MAX_ERROR_EVIDENCE_BYTES = spec.limits.error_evidence_bytes;
export const MAX_DEADLINE_MS = spec.limits.deadline_ms;
export const OPERATIONS = new Set(spec.operations);
export const OUTCOMES = new Set([
  "not_started",
  "completed",
  "unknown_outcome",
]);

const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const TURN_STATES = new Set([
  "busy",
  "settled",
  "approval_required",
  "input_required",
  "indeterminate",
  "navigation_drift",
  "failed",
]);
const MAX_TRANSCRIPT_TURNS = 256;
const MAX_APPROVAL_CARDS = 8;
const MAX_ENABLED_SKILLS = 256;
const MAX_TURN_TEXT_BYTES = 16384;
const CREDENTIAL_KEYS = new Set([
  "authorization",
  "cookie",
  "credential",
  "credentials",
  "password",
  "secret",
  "token",
]);

export class BoundaryError extends Error {
  constructor(
    code,
    message,
    { outcome = "not_started", retryable = false, evidence = {} } = {},
  ) {
    super(message);
    this.name = "BoundaryError";
    this.code = code;
    this.outcome = outcome;
    this.retryable = retryable;
    this.evidence = evidence;
  }
}

export function parseRequestText(text) {
  if (typeof text !== "string") {
    throw new BoundaryError("INVALID_REQUEST", "Request input must be text");
  }
  if (Buffer.byteLength(text) > MAX_REQUEST_BYTES) {
    throw new BoundaryError(
      "REQUEST_TOO_LARGE",
      `Request exceeds the ${MAX_REQUEST_BYTES}-byte limit`,
    );
  }
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    throw new BoundaryError("INVALID_JSON", "Request is not valid JSON");
  }
  return validateRequest(value);
}

export function validateRequest(value) {
  const request = object(value, "request");
  exactKeys(
    request,
    [
      "protocol_version",
      "request_id",
      "operation",
      "session",
      "deadline_ms",
      "payload",
    ],
    "request",
  );
  rejectCredentialKeys(request);
  exactInteger(
    request.protocol_version,
    PROTOCOL_VERSION,
    "request.protocol_version",
  );
  identifier(request.request_id, "request.request_id");
  if (!OPERATIONS.has(request.operation)) {
    throw new BoundaryError("UNSUPPORTED_OPERATION", "Operation is unsupported");
  }
  boundedInteger(
    request.deadline_ms,
    1,
    MAX_DEADLINE_MS,
    "request.deadline_ms",
  );
  validateSession(request.session);
  object(request.payload, "request.payload");
  if (
    ["session.attach", "session.inspect", "session.detach"].includes(
      request.operation,
    )
  ) {
    exactKeys(request.payload, [], "request.payload");
  }
  if (["project.inspect", "agent_context.inspect"].includes(request.operation)) {
    exactKeys(request.payload, ["project_id"], "request.payload");
    identifier(request.payload.project_id, "request.payload.project_id");
  }
  if (request.operation === "chat.inspect") {
    exactKeys(
      request.payload,
      ["project_id", "chat_id", "root_frame_id"],
      "request.payload",
      ["root_frame_id"],
    );
    identifier(request.payload.project_id, "request.payload.project_id");
    identifier(request.payload.chat_id, "request.payload.chat_id");
    if ("root_frame_id" in request.payload) {
      identifier(
        request.payload.root_frame_id,
        "request.payload.root_frame_id",
      );
    }
  }
  // Redundant when reached via parseRequestText (raw text was already size-checked), but
  // validateRequest is exported and may be called directly on an untrusted object — keep it.
  if (jsonBytes(request) > MAX_REQUEST_BYTES) {
    throw new BoundaryError(
      "REQUEST_TOO_LARGE",
      `Request exceeds the ${MAX_REQUEST_BYTES}-byte limit`,
    );
  }
  return request;
}

export function completedResponse(request, result, elapsedMs) {
  const validatedResult = object(result, "result");
  validateOperationResult(request.operation, validatedResult);
  return validateResponse({
    protocol_version: PROTOCOL_VERSION,
    request_id: request.request_id,
    operation: request.operation,
    outcome: "completed",
    elapsed_ms: elapsedMs,
    result: validatedResult,
  });
}

function validateOperationResult(operation, result) {
  if (operation === "session.detach") {
    exactKeys(result, ["detached"], "response.result");
    boolean(result.detached, "response.result.detached");
    return;
  }
  if (operation === "project.inspect") {
    validateProjectObservation(result);
    return;
  }
  if (operation === "chat.inspect") {
    validateChatObservation(result);
    return;
  }
  if (operation === "agent_context.inspect") {
    validateContextObservation(result);
    return;
  }
  if (!["session.attach", "session.inspect"].includes(operation)) return;
  exactKeys(
    result,
    ["authenticated", "origin", "profile_ready"],
    "response.result",
  );
  boolean(result.authenticated, "response.result.authenticated");
  origin(result.origin, "response.result.origin");
  boolean(result.profile_ready, "response.result.profile_ready");
}

function validateProjectObservation(result) {
  exactKeys(
    result,
    [
      "project_id",
      "verified",
      "composer_empty",
      "user_turn_count",
      "root_frame_id",
      "root_state",
    ],
    "response.result",
  );
  identifier(result.project_id, "response.result.project_id");
  boolean(result.verified, "response.result.verified");
  boolean(result.composer_empty, "response.result.composer_empty");
  boundedInteger(
    result.user_turn_count,
    0,
    1000000,
    "response.result.user_turn_count",
  );
  nullableIdentifier(result.root_frame_id, "response.result.root_frame_id");
  nullableIdentifier(result.root_state, "response.result.root_state");
  if ((result.root_frame_id === null) !== (result.root_state === null)) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "Project root identity and state must be jointly present or absent",
    );
  }
}

function validateChatObservation(result) {
  exactKeys(
    result,
    [
      "project_id",
      "chat_id",
      "transcript",
      "user_turn_count",
      "composer_empty",
      "root_frame_id",
      "response_control_id",
      "current_turn_state",
      "approval_cards",
    ],
    "response.result",
  );
  identifier(result.project_id, "response.result.project_id");
  identifier(result.chat_id, "response.result.chat_id");
  array(result.transcript, MAX_TRANSCRIPT_TURNS, "response.result.transcript");
  const turnIds = new Set();
  let observedUsers = 0;
  for (const [index, turn] of result.transcript.entries()) {
    const path = `response.result.transcript[${index}]`;
    object(turn, path);
    exactKeys(turn, ["turn_id", "role", "text", "truncated"], path);
    identifier(turn.turn_id, `${path}.turn_id`);
    if (turnIds.has(turn.turn_id)) {
      throw new BoundaryError("INVALID_RESPONSE", "Transcript turn IDs must be unique");
    }
    turnIds.add(turn.turn_id);
    if (!["user", "assistant"].includes(turn.role)) {
      throw new BoundaryError("INVALID_RESPONSE", `${path}.role is invalid`);
    }
    if (turn.role === "user") observedUsers += 1;
    boundedString(turn.text, MAX_TURN_TEXT_BYTES, `${path}.text`);
    boolean(turn.truncated, `${path}.truncated`);
  }
  boundedInteger(
    result.user_turn_count,
    0,
    1000000,
    "response.result.user_turn_count",
  );
  if (result.user_turn_count !== observedUsers) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "Chat user-turn count contradicts the transcript",
    );
  }
  boolean(result.composer_empty, "response.result.composer_empty");
  nullableIdentifier(result.root_frame_id, "response.result.root_frame_id");
  nullableIdentifier(
    result.response_control_id,
    "response.result.response_control_id",
  );
  if (!TURN_STATES.has(result.current_turn_state)) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "response.result.current_turn_state is invalid",
    );
  }
  array(
    result.approval_cards,
    MAX_APPROVAL_CARDS,
    "response.result.approval_cards",
  );
  const cardIds = new Set();
  for (const [index, card] of result.approval_cards.entries()) {
    const path = `response.result.approval_cards[${index}]`;
    object(card, path);
    exactKeys(card, ["card_id", "fingerprint", "title", "kind"], path);
    identifier(card.card_id, `${path}.card_id`);
    if (cardIds.has(card.card_id)) {
      throw new BoundaryError("INVALID_RESPONSE", "Approval card IDs must be unique");
    }
    cardIds.add(card.card_id);
    sha256(card.fingerprint, `${path}.fingerprint`);
    boundedText(card.title, 512, `${path}.title`);
    identifier(card.kind, `${path}.kind`);
  }
  if (
    (result.current_turn_state === "approval_required") !==
    (result.approval_cards.length > 0)
  ) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "Approval state contradicts observed approval cards",
    );
  }
  if (
    result.response_control_id !== null &&
    !result.transcript.some(
      (turn) =>
        turn.turn_id === result.response_control_id && turn.role === "assistant",
    )
  ) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "Response-control identity must name an observed assistant turn",
    );
  }
  if (result.root_frame_id === null && result.transcript.length > 0) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "A rootless chat cannot contain transcript turns",
    );
  }
}

function validateContextObservation(result) {
  exactKeys(
    result,
    ["project_id", "enabled_skills", "context_hash"],
    "response.result",
  );
  identifier(result.project_id, "response.result.project_id");
  array(
    result.enabled_skills,
    MAX_ENABLED_SKILLS,
    "response.result.enabled_skills",
  );
  const names = new Set();
  for (const [index, skill] of result.enabled_skills.entries()) {
    boundedText(skill, 256, `response.result.enabled_skills[${index}]`);
    if (names.has(skill)) {
      throw new BoundaryError("INVALID_RESPONSE", "Enabled skills must be unique");
    }
    names.add(skill);
  }
  if (
    [...result.enabled_skills].sort().join("\0") !==
    result.enabled_skills.join("\0")
  ) {
    throw new BoundaryError("INVALID_RESPONSE", "Enabled skills must be sorted");
  }
  sha256(result.context_hash, "response.result.context_hash");
}

export function errorResponse(request, error, elapsedMs) {
  const boundaryError = error instanceof BoundaryError
    ? error
    : new BoundaryError("BOUNDARY_FAILURE", "Browser boundary failed");
  return validateResponse({
    protocol_version: PROTOCOL_VERSION,
    request_id: request.request_id,
    operation: request.operation,
    outcome: boundaryError.outcome,
    elapsed_ms: elapsedMs,
    error: {
      code: boundaryError.code,
      message: boundaryError.message,
      retryable: boundaryError.retryable,
      evidence: boundaryError.evidence,
    },
  });
}

export function validateResponse(value) {
  const response = object(value, "response");
  exactKeys(
    response,
    [
      "protocol_version",
      "request_id",
      "operation",
      "outcome",
      "elapsed_ms",
      "result",
      "error",
    ],
    "response",
    ["result", "error"],
  );
  rejectCredentialKeys(response);
  exactInteger(
    response.protocol_version,
    PROTOCOL_VERSION,
    "response.protocol_version",
  );
  identifier(response.request_id, "response.request_id");
  if (!OPERATIONS.has(response.operation)) {
    throw new BoundaryError("INVALID_RESPONSE", "Response operation is unsupported");
  }
  if (!OUTCOMES.has(response.outcome)) {
    throw new BoundaryError("INVALID_RESPONSE", "Response outcome is unsupported");
  }
  boundedInteger(
    response.elapsed_ms,
    0,
    MAX_DEADLINE_MS,
    "response.elapsed_ms",
  );
  if (response.outcome === "completed") {
    if (!("result" in response) || "error" in response) {
      throw new BoundaryError(
        "INVALID_RESPONSE",
        "Completed response must contain only result",
      );
    }
    object(response.result, "response.result");
  } else {
    if (!("error" in response) || "result" in response) {
      throw new BoundaryError(
        "INVALID_RESPONSE",
        "Non-completed response must contain only error",
      );
    }
    validateError(response.error, response.outcome);
  }
  if (jsonBytes(response) > MAX_RESPONSE_BYTES) {
    throw new BoundaryError(
      "RESPONSE_TOO_LARGE",
      `Response exceeds the ${MAX_RESPONSE_BYTES}-byte limit`,
    );
  }
  return response;
}

export function serializeResponse(response) {
  const text = JSON.stringify(validateResponse(response));
  if (Buffer.byteLength(text) > MAX_RESPONSE_BYTES) {
    throw new BoundaryError(
      "RESPONSE_TOO_LARGE",
      `Response exceeds the ${MAX_RESPONSE_BYTES}-byte limit`,
    );
  }
  return `${text}\n`;
}

function validateSession(value) {
  const session = object(value, "request.session");
  exactKeys(session, ["session_id", "origin"], "request.session");
  identifier(session.session_id, "request.session.session_id");
  origin(session.origin, "request.session.origin");
}

function validateError(value, outcome) {
  const error = object(value, "response.error");
  exactKeys(
    error,
    ["code", "message", "retryable", "evidence"],
    "response.error",
  );
  identifier(error.code, "response.error.code");
  boundedText(error.message, 4096, "response.error.message");
  if (typeof error.retryable !== "boolean") {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "response.error.retryable must be boolean",
    );
  }
  const evidence = object(error.evidence, "response.error.evidence");
  if (jsonBytes(evidence) > MAX_ERROR_EVIDENCE_BYTES) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "Response error evidence is too large",
    );
  }
  if (outcome === "unknown_outcome" && error.retryable) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "Unknown outcomes must be non-retryable",
    );
  }
}

function rejectCredentialKeys(value, path = "request") {
  if (Array.isArray(value)) {
    value.forEach((item, index) =>
      rejectCredentialKeys(item, `${path}[${index}]`));
    return;
  }
  if (!value || typeof value !== "object") return;
  for (const [key, item] of Object.entries(value)) {
    if (CREDENTIAL_KEYS.has(key.toLowerCase())) {
      throw new BoundaryError(
        "CREDENTIALS_FORBIDDEN",
        `${path}.${key} is forbidden in boundary JSON`,
      );
    }
    rejectCredentialKeys(item, `${path}.${key}`);
  }
}

function exactKeys(value, allowed, path, optional = []) {
  const allowedSet = new Set(allowed);
  const optionalSet = new Set(optional);
  const unknown = Object.keys(value).filter((key) => !allowedSet.has(key));
  const missing = allowed.filter(
    (key) => !optionalSet.has(key) && !(key in value),
  );
  if (unknown.length || missing.length) {
    throw new BoundaryError(
      "INVALID_FIELDS",
      `${path} fields are not exact`,
      { evidence: { unknown: unknown.sort(), missing: missing.sort() } },
    );
  }
}

function object(value, path) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new BoundaryError("INVALID_TYPE", `${path} must be an object`);
  }
  return value;
}

function identifier(value, path) {
  if (typeof value !== "string" || !IDENTIFIER.test(value)) {
    throw new BoundaryError("INVALID_IDENTIFIER", `${path} is invalid`);
  }
  return value;
}

function nullableIdentifier(value, path) {
  if (value === null) return value;
  return identifier(value, path);
}

function sha256(value, path) {
  if (typeof value !== "string" || !SHA256.test(value)) {
    throw new BoundaryError("INVALID_TEXT", `${path} must be a SHA-256 digest`);
  }
  return value;
}

function boundedText(value, maximumBytes, path) {
  if (typeof value !== "string" || !value || Buffer.byteLength(value) > maximumBytes) {
    throw new BoundaryError("INVALID_TEXT", `${path} is invalid`);
  }
  return value;
}

function boundedString(value, maximumBytes, path) {
  if (typeof value !== "string" || Buffer.byteLength(value) > maximumBytes) {
    throw new BoundaryError("INVALID_TEXT", `${path} is invalid`);
  }
  return value;
}

function array(value, maximumLength, path) {
  if (!Array.isArray(value) || value.length > maximumLength) {
    throw new BoundaryError("INVALID_TYPE", `${path} must be a bounded array`);
  }
  return value;
}

function boundedInteger(value, minimum, maximum, path) {
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new BoundaryError("INVALID_INTEGER", `${path} is out of bounds`);
  }
  return value;
}

function boolean(value, path) {
  if (typeof value !== "boolean") {
    throw new BoundaryError("INVALID_BOOLEAN", `${path} must be boolean`);
  }
  return value;
}

function exactInteger(value, expected, path) {
  if (!Number.isInteger(value) || value !== expected) {
    throw new BoundaryError("INVALID_INTEGER", `${path} must equal ${expected}`);
  }
  return value;
}

function origin(value, path) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new BoundaryError("INVALID_ORIGIN", `${path} is invalid`);
  }
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash ||
    value !== parsed.origin
  ) {
    throw new BoundaryError("INVALID_ORIGIN", `${path} must be a bare HTTP origin`);
  }
  return parsed.origin;
}

function jsonBytes(value) {
  return Buffer.byteLength(JSON.stringify(value));
}
