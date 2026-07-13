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
const MAX_PROMPT_BYTES = 65536;
const MAX_PROJECT_NAME_BYTES = 640;
const MAX_SOURCE_PATH_BYTES = 4096;
const MAX_MODEL_LABEL_BYTES = 128;
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
  if (request.operation === "project.create") {
    exactKeys(request.payload, ["name"], "request.payload");
    boundedText(
      request.payload.name,
      MAX_PROJECT_NAME_BYTES,
      "request.payload.name",
    );
    if (
      request.payload.name.length > 160 ||
      request.payload.name.trim() !== request.payload.name
    ) {
      throw new BoundaryError("INVALID_TEXT", "request.payload.name is invalid");
    }
  }
  if (request.operation === "attachment.upload") {
    exactKeys(
      request.payload,
      ["project_id", "chat_id", "source_path"],
      "request.payload",
    );
    identifier(request.payload.project_id, "request.payload.project_id");
    identifier(request.payload.chat_id, "request.payload.chat_id");
    boundedText(
      request.payload.source_path,
      MAX_SOURCE_PATH_BYTES,
      "request.payload.source_path",
    );
    if (
      !request.payload.source_path.startsWith("/") ||
      request.payload.source_path.includes("\0") ||
      request.payload.source_path.endsWith("/")
    ) {
      throw new BoundaryError(
        "INVALID_TEXT",
        "request.payload.source_path must be an absolute file path",
      );
    }
  }
  if (request.operation === "chat.new") {
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
  if (request.operation === "model.select") {
    exactKeys(
      request.payload,
      ["project_id", "chat_id", "model_label"],
      "request.payload",
    );
    identifier(request.payload.project_id, "request.payload.project_id");
    identifier(request.payload.chat_id, "request.payload.chat_id");
    boundedText(
      request.payload.model_label,
      MAX_MODEL_LABEL_BYTES,
      "request.payload.model_label",
    );
    if (
      request.payload.model_label.trim() !== request.payload.model_label ||
      /[\u0000-\u001f\u007f]/u.test(request.payload.model_label)
    ) {
      throw new BoundaryError("INVALID_TEXT", "request.payload.model_label is invalid");
    }
  }
  if (request.operation === "turn.submit_wait") {
    exactKeys(
      request.payload,
      [
        "project_id",
        "chat_id",
        "root_mode",
        "prompt",
        "authored_prompt_sha256",
        "root_frame_id",
      ],
      "request.payload",
      ["root_frame_id"],
    );
    identifier(request.payload.project_id, "request.payload.project_id");
    identifier(request.payload.chat_id, "request.payload.chat_id");
    if (!["new", "existing"].includes(request.payload.root_mode)) {
      throw new BoundaryError("INVALID_TEXT", "request.payload.root_mode is invalid");
    }
    boundedText(request.payload.prompt, MAX_PROMPT_BYTES, "request.payload.prompt");
    sha256(
      request.payload.authored_prompt_sha256,
      "request.payload.authored_prompt_sha256",
    );
    const hasRoot = "root_frame_id" in request.payload;
    if (request.payload.root_mode === "new" && hasRoot) {
      throw new BoundaryError(
        "INVALID_FIELDS",
        "A new-root submission cannot include root_frame_id",
      );
    }
    if (request.payload.root_mode === "existing" && !hasRoot) {
      throw new BoundaryError(
        "INVALID_FIELDS",
        "An existing-root submission requires root_frame_id",
      );
    }
    if (hasRoot) {
      identifier(request.payload.root_frame_id, "request.payload.root_frame_id");
    }
  }
  if (request.operation === "turn.wait") {
    exactKeys(
      request.payload,
      ["project_id", "chat_id", "continuation"],
      "request.payload",
    );
    identifier(request.payload.project_id, "request.payload.project_id");
    identifier(request.payload.chat_id, "request.payload.chat_id");
    validateTurnContinuation(
      object(request.payload.continuation, "request.payload.continuation"),
      "request.payload.continuation",
    );
    if (
      request.payload.continuation.project_id !== request.payload.project_id ||
      request.payload.continuation.chat_id !== request.payload.chat_id
    ) {
      throw new BoundaryError(
        "INVALID_RESPONSE",
        "Continuation identity contradicts the wait request",
      );
    }
  }
  if (request.operation === "approval.resolve") {
    exactKeys(
      request.payload,
      [
        "project_id",
        "chat_id",
        "root_frame_id",
        "card_id",
        "decision",
        "expected_fingerprint",
      ],
      "request.payload",
    );
    for (const key of ["project_id", "chat_id", "root_frame_id", "card_id"]) {
      identifier(request.payload[key], `request.payload.${key}`);
    }
    if (!["allow_for_conversation", "deny"].includes(request.payload.decision)) {
      throw new BoundaryError("INVALID_TEXT", "request.payload.decision is invalid");
    }
    sha256(
      request.payload.expected_fingerprint,
      "request.payload.expected_fingerprint",
    );
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
  validateOperationResult(request.operation, validatedResult, request.payload);
  return validateResponse({
    protocol_version: PROTOCOL_VERSION,
    request_id: request.request_id,
    operation: request.operation,
    outcome: "completed",
    elapsed_ms: elapsedMs,
    result: validatedResult,
  });
}

function validateOperationResult(operation, result, requestPayload) {
  if (operation === "session.detach") {
    exactKeys(result, ["detached"], "response.result");
    boolean(result.detached, "response.result.detached");
    return;
  }
  if (["project.inspect", "project.create"].includes(operation)) {
    validateProjectObservation(result);
    return;
  }
  if (["chat.inspect", "chat.new"].includes(operation)) {
    validateChatObservation(result);
    if (operation === "chat.new") {
      if (
        result.project_id !== requestPayload.project_id ||
        result.root_frame_id !== null ||
        result.response_control_id !== null ||
        result.user_turn_count !== 0 ||
        result.composer_empty !== true ||
        result.transcript.length !== 0 ||
        result.current_turn_state !== "indeterminate" ||
        result.approval_cards.length !== 0
      ) {
        throw new BoundaryError(
          "INVALID_RESPONSE",
          "New chat result is not a verified blank chat",
        );
      }
    }
    return;
  }
  if (operation === "attachment.upload") {
    exactKeys(
      result,
      ["project_id", "chat_id", "filename", "accepted"],
      "response.result",
    );
    identifier(result.project_id, "response.result.project_id");
    identifier(result.chat_id, "response.result.chat_id");
    boundedText(result.filename, 1024, "response.result.filename");
    if (
      result.project_id !== requestPayload.project_id ||
      result.chat_id !== requestPayload.chat_id ||
      result.filename !== requestPayload.source_path.split("/").at(-1) ||
      result.accepted !== true
    ) {
      throw new BoundaryError(
        "INVALID_RESPONSE",
        "Attachment result does not correlate to its request",
      );
    }
    return;
  }
  if (operation === "model.select") {
    exactKeys(
      result,
      [
        "project_id",
        "chat_id",
        "model_label",
        "previous_model_label",
        "changed",
        "confirmed",
      ],
      "response.result",
    );
    identifier(result.project_id, "response.result.project_id");
    identifier(result.chat_id, "response.result.chat_id");
    boundedText(result.model_label, MAX_MODEL_LABEL_BYTES, "response.result.model_label");
    boundedText(
      result.previous_model_label,
      MAX_MODEL_LABEL_BYTES,
      "response.result.previous_model_label",
    );
    boolean(result.changed, "response.result.changed");
    boolean(result.confirmed, "response.result.confirmed");
    if (
      result.project_id !== requestPayload.project_id ||
      result.chat_id !== requestPayload.chat_id ||
      result.model_label !== requestPayload.model_label ||
      result.confirmed !== true ||
      result.changed !== (result.previous_model_label !== result.model_label)
    ) {
      throw new BoundaryError(
        "INVALID_RESPONSE",
        "Model selection result does not correlate to its request",
      );
    }
    return;
  }
  if (operation === "agent_context.inspect") {
    validateContextObservation(result);
    return;
  }
  if (["turn.submit_wait", "turn.wait"].includes(operation)) {
    validateTurnResult(result);
    validateTurnCorrelation(operation, result, requestPayload);
    return;
  }
  if (operation === "approval.resolve") {
    validateApprovalResolved(result);
    for (const key of [
      "project_id",
      "chat_id",
      "root_frame_id",
      "card_id",
      "decision",
    ]) {
      if (result[key] !== requestPayload[key]) {
        throw new BoundaryError(
          "INVALID_RESPONSE",
          "Approval result does not correlate to its request",
        );
      }
    }
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
  validateApprovalCards(
    result.approval_cards,
    "response.result.approval_cards",
  );
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

function validateTurnResult(result) {
  exactKeys(
    result,
    [
      "project_id",
      "chat_id",
      "root_frame_id",
      "turn_state",
      "root_created",
      "delivery",
      "settled",
      "approval",
      "continuation",
    ],
    "response.result",
  );
  identifier(result.project_id, "response.result.project_id");
  identifier(result.chat_id, "response.result.chat_id");
  nullableIdentifier(result.root_frame_id, "response.result.root_frame_id");
  if (!TURN_STATES.has(result.turn_state)) {
    throw new BoundaryError("INVALID_RESPONSE", "Turn result state is invalid");
  }
  boolean(result.root_created, "response.result.root_created");

  if (result.delivery !== null) {
    validateDeliveryProof(
      object(result.delivery, "response.result.delivery"),
      "response.result.delivery",
    );
    if (result.delivery.root_frame_id !== result.root_frame_id) {
      throw new BoundaryError(
        "INVALID_RESPONSE",
        "Delivery root contradicts the turn result",
      );
    }
  }
  if (result.settled !== null) {
    validateSettledProof(
      object(result.settled, "response.result.settled"),
      "response.result.settled",
    );
  }
  if (result.approval !== null) {
    validateApprovalObservation(
      object(result.approval, "response.result.approval"),
      "response.result.approval",
    );
  }
  if (result.continuation !== null) {
    validateTurnContinuation(
      object(result.continuation, "response.result.continuation"),
      "response.result.continuation",
    );
  }

  const delivered = result.delivery !== null;
  const isSettled = result.turn_state === "settled";
  if (isSettled !== (result.settled !== null)) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "Settled state contradicts the settlement proof",
    );
  }
  if ((result.turn_state === "approval_required") !== (result.approval !== null)) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "Approval state contradicts the approval observation",
    );
  }
  if (delivered && !isSettled && result.continuation === null) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "A delivered unsettled turn requires a continuation",
    );
  }
  if ((!delivered || isSettled) && result.continuation !== null) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "Continuation is only valid for delivered unsettled turns",
    );
  }
  if (delivered && result.root_frame_id === null) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "A delivered turn requires a root identity",
    );
  }
  if (result.continuation !== null) {
    const continuation = result.continuation;
    if (
      continuation.project_id !== result.project_id ||
      continuation.chat_id !== result.chat_id ||
      continuation.root_frame_id !== result.root_frame_id ||
      continuation.authored_prompt_sha256 !==
        result.delivery.authored_prompt_sha256 ||
      continuation.delivery_text_sha256 !==
        result.delivery.delivery_text_sha256 ||
      continuation.normalized_user_turn_id !==
        result.delivery.normalized_user_turn_id
    ) {
      throw new BoundaryError(
        "INVALID_RESPONSE",
        "Continuation contradicts the delivery proof",
      );
    }
  }
}

function validateDeliveryProof(value, path) {
  exactKeys(
    value,
    [
      "root_frame_id",
      "authored_prompt_sha256",
      "delivery_text_sha256",
      "normalized_user_turn_id",
    ],
    path,
  );
  identifier(value.root_frame_id, `${path}.root_frame_id`);
  sha256(value.authored_prompt_sha256, `${path}.authored_prompt_sha256`);
  sha256(value.delivery_text_sha256, `${path}.delivery_text_sha256`);
  identifier(value.normalized_user_turn_id, `${path}.normalized_user_turn_id`);
}

function validateSettledProof(value, path) {
  exactKeys(
    value,
    ["stop_hidden", "stable_samples", "new_response_control_id"],
    path,
  );
  if (value.stop_hidden !== true) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      `${path}.stop_hidden must be true`,
    );
  }
  boundedInteger(value.stable_samples, 2, 100, `${path}.stable_samples`);
  identifier(
    value.new_response_control_id,
    `${path}.new_response_control_id`,
  );
}

function validateTurnContinuation(value, path) {
  exactKeys(
    value,
    [
      "project_id",
      "chat_id",
      "root_frame_id",
      "authored_prompt_sha256",
      "delivery_text_sha256",
      "normalized_user_turn_id",
      "baseline_response_control_id",
    ],
    path,
  );
  identifier(value.project_id, `${path}.project_id`);
  identifier(value.chat_id, `${path}.chat_id`);
  identifier(value.root_frame_id, `${path}.root_frame_id`);
  sha256(value.authored_prompt_sha256, `${path}.authored_prompt_sha256`);
  sha256(value.delivery_text_sha256, `${path}.delivery_text_sha256`);
  identifier(value.normalized_user_turn_id, `${path}.normalized_user_turn_id`);
  nullableIdentifier(
    value.baseline_response_control_id,
    `${path}.baseline_response_control_id`,
  );
}

function validateApprovalObservation(value, path) {
  exactKeys(value, ["cards"], path);
  validateApprovalCards(value.cards, `${path}.cards`);
  if (value.cards.length !== 1) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "Approval observation must contain one unique card",
    );
  }
}

function validateApprovalCards(cards, path) {
  array(cards, MAX_APPROVAL_CARDS, path);
  const cardIds = new Set();
  for (const [index, cardValue] of cards.entries()) {
    const cardPath = `${path}[${index}]`;
    const card = object(cardValue, cardPath);
    exactKeys(card, ["card_id", "fingerprint", "title", "kind"], cardPath);
    identifier(card.card_id, `${cardPath}.card_id`);
    if (cardIds.has(card.card_id)) {
      throw new BoundaryError("INVALID_RESPONSE", "Approval card IDs must be unique");
    }
    cardIds.add(card.card_id);
    sha256(card.fingerprint, `${cardPath}.fingerprint`);
    boundedText(card.title, 512, `${cardPath}.title`);
    identifier(card.kind, `${cardPath}.kind`);
  }
}

function validateApprovalResolved(result) {
  exactKeys(
    result,
    [
      "project_id",
      "chat_id",
      "root_frame_id",
      "card_id",
      "decision",
      "verified_cleared",
    ],
    "response.result",
  );
  for (const key of ["project_id", "chat_id", "root_frame_id", "card_id"]) {
    identifier(result[key], `response.result.${key}`);
  }
  if (!["allow_for_conversation", "deny"].includes(result.decision)) {
    throw new BoundaryError("INVALID_RESPONSE", "Approval decision is invalid");
  }
  if (result.verified_cleared !== true) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "Resolved approval must be verified cleared",
    );
  }
}

function validateTurnCorrelation(operation, result, payload) {
  if (
    result.project_id !== payload.project_id ||
    result.chat_id !== payload.chat_id
  ) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "Turn result does not correlate to its request",
    );
  }
  const expected = operation === "turn.submit_wait"
    ? {
        authored_prompt_sha256: payload.authored_prompt_sha256,
        root_frame_id: payload.root_frame_id ?? null,
      }
    : {
        authored_prompt_sha256:
          payload.continuation.authored_prompt_sha256,
        delivery_text_sha256:
          payload.continuation.delivery_text_sha256,
        normalized_user_turn_id:
          payload.continuation.normalized_user_turn_id,
        root_frame_id: payload.continuation.root_frame_id,
      };
  if (
    !result.delivery ||
    result.delivery.authored_prompt_sha256 !==
      expected.authored_prompt_sha256 ||
    ("delivery_text_sha256" in expected &&
      result.delivery.delivery_text_sha256 !== expected.delivery_text_sha256) ||
    ("normalized_user_turn_id" in expected &&
      result.delivery.normalized_user_turn_id !==
        expected.normalized_user_turn_id) ||
    (operation === "turn.submit_wait" &&
      payload.root_mode === "existing" &&
      result.root_frame_id !== expected.root_frame_id) ||
    (operation === "turn.wait" &&
      result.root_frame_id !== expected.root_frame_id)
  ) {
    throw new BoundaryError(
      "INVALID_RESPONSE",
      "Turn proof does not correlate to its request",
    );
  }
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
