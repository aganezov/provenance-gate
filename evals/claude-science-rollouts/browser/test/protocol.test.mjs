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

function chatObservation(overrides = {}) {
  return {
    project_id: "project-001",
    chat_id: "chat-001",
    transcript: [
      { turn_id: "turn-user", role: "user", text: "Question", truncated: false },
      {
        turn_id: "turn-assistant",
        role: "assistant",
        text: "Answer",
        truncated: false,
      },
    ],
    user_turn_count: 1,
    composer_empty: true,
    root_frame_id: "root-001",
    response_control_id: "turn-assistant",
    current_turn_state: "settled",
    approval_cards: [],
    ...overrides,
  };
}

function continuation(overrides = {}) {
  return {
    project_id: "project-001",
    chat_id: "chat-001",
    root_frame_id: "root-001",
    authored_prompt_sha256: "a".repeat(64),
    delivery_text_sha256: "b".repeat(64),
    normalized_user_turn_id: "turn-user-new",
    baseline_response_control_id: "turn-assistant-old",
    ...overrides,
  };
}

function turnResult(overrides = {}) {
  return {
    project_id: "project-001",
    chat_id: "chat-001",
    root_frame_id: "root-001",
    turn_state: "busy",
    root_created: false,
    delivery: {
      root_frame_id: "root-001",
      authored_prompt_sha256: "a".repeat(64),
      delivery_text_sha256: "b".repeat(64),
      normalized_user_turn_id: "turn-user-new",
    },
    settled: null,
    approval: null,
    continuation: continuation(),
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

test("session inspection result fields are exact and typed", () => {
  const parsed = parseRequestText(JSON.stringify(request()));
  assert.throws(
    () => completedResponse(
      parsed,
      { authenticated: true, origin: parsed.session.origin },
      12,
    ),
    (error) => error instanceof BoundaryError && error.code === "INVALID_FIELDS",
  );
  assert.throws(
    () => completedResponse(
      parsed,
      {
        authenticated: "yes",
        origin: parsed.session.origin,
        profile_ready: true,
      },
      12,
    ),
    (error) => error instanceof BoundaryError && error.code === "INVALID_BOOLEAN",
  );
});

test("session lifecycle payloads are empty and detach results are exact", () => {
  for (const operation of ["session.attach", "session.inspect", "session.detach"]) {
    assert.throws(
      () => parseRequestText(
        JSON.stringify(request({ operation, payload: { extra: true } })),
      ),
      (error) =>
        error instanceof BoundaryError && error.code === "INVALID_FIELDS",
    );
  }
  const parsed = parseRequestText(
    JSON.stringify(request({ operation: "session.detach" })),
  );
  assert.deepEqual(completedResponse(parsed, { detached: true }, 3).result, {
    detached: true,
  });
  assert.throws(
    () => completedResponse(parsed, { detached: "yes" }, 3),
    (error) => error instanceof BoundaryError && error.code === "INVALID_BOOLEAN",
  );
});

test("model selection payload and confirmation are exact and correlated", () => {
  const parsed = parseRequestText(
    JSON.stringify(
      request({
        operation: "model.select",
        payload: {
          project_id: "project-001",
          chat_id: "chat-001",
          model_label: "Research Fast",
        },
      }),
    ),
  );
  const response = completedResponse(
    parsed,
    {
      project_id: "project-001",
      chat_id: "chat-001",
      model_label: "Research Fast",
      previous_model_label: "Research Fast",
      changed: false,
      confirmed: true,
    },
    12,
  );
  assert.equal(response.result.changed, false);
  assert.throws(
    () =>
      parseRequestText(
        JSON.stringify(
          request({
            operation: "model.select",
            payload: {
              project_id: "project-001",
              chat_id: "chat-001",
              model_label: " leading",
            },
          }),
        ),
      ),
    (error) => error instanceof BoundaryError && error.code === "INVALID_TEXT",
  );
  assert.throws(
    () => parseRequestText(JSON.stringify(request({
      operation: "model.select",
      payload: {
        project_id: "project-001",
        chat_id: "chat-001",
        model_label: "Research\nFast",
      },
    }))),
    (error) => error instanceof BoundaryError && error.code === "INVALID_TEXT",
  );
  assert.throws(
    () => completedResponse(parsed, {
      project_id: "project-001",
      chat_id: "chat-001",
      model_label: "Research Fast",
      previous_model_label: "Research Fast",
      changed: true,
      confirmed: true,
    }, 12),
    (error) => error instanceof BoundaryError && error.code === "INVALID_RESPONSE",
  );
});

test("G3a request payloads are exact and identity typed", () => {
  for (const operation of ["project.inspect", "agent_context.inspect"]) {
    const parsed = parseRequestText(JSON.stringify(request({
      operation,
      payload: { project_id: "project-001" },
    })));
    assert.equal(parsed.payload.project_id, "project-001");
  }
  const rooted = parseRequestText(JSON.stringify(request({
    operation: "chat.inspect",
    payload: {
      project_id: "project-001",
      chat_id: "chat-001",
      root_frame_id: "root-001",
    },
  })));
  assert.equal(rooted.payload.root_frame_id, "root-001");
  assert.throws(
    () => parseRequestText(JSON.stringify(request({
      operation: "chat.inspect",
      payload: { project_id: "project-001", chat_id: "invalid chat" },
    }))),
    (error) =>
      error instanceof BoundaryError && error.code === "INVALID_IDENTIFIER",
  );
});

test("G3b requests enforce root modes, hashes, continuations, and decisions", () => {
  const submit = parseRequestText(JSON.stringify(request({
    operation: "turn.submit_wait",
    payload: {
      project_id: "project-001",
      chat_id: "chat-001",
      root_mode: "new",
      prompt: "Do one thing",
      authored_prompt_sha256: "a".repeat(64),
    },
  })));
  assert.equal(submit.payload.root_mode, "new");
  assert.throws(
    () => parseRequestText(JSON.stringify(request({
      operation: "turn.submit_wait",
      payload: { ...submit.payload, root_frame_id: "root-001" },
    }))),
    (error) => error instanceof BoundaryError && error.code === "INVALID_FIELDS",
  );
  assert.throws(
    () => parseRequestText(JSON.stringify(request({
      operation: "turn.submit_wait",
      payload: { ...submit.payload, root_mode: "existing" },
    }))),
    (error) => error instanceof BoundaryError && error.code === "INVALID_FIELDS",
  );
  assert.doesNotThrow(() => parseRequestText(JSON.stringify(request({
    operation: "turn.wait",
    payload: {
      project_id: "project-001",
      chat_id: "chat-001",
      continuation: continuation(),
    },
  }))));
  assert.throws(
    () => parseRequestText(JSON.stringify(request({
      operation: "turn.wait",
      payload: {
        project_id: "project-other",
        chat_id: "chat-001",
        continuation: continuation(),
      },
    }))),
    (error) => error instanceof BoundaryError,
  );
  assert.doesNotThrow(() => parseRequestText(JSON.stringify(request({
    operation: "approval.resolve",
    payload: {
      project_id: "project-001",
      chat_id: "chat-001",
      root_frame_id: "root-001",
      card_id: "approval:abc:0",
      decision: "allow_for_conversation",
      expected_fingerprint: "c".repeat(64),
    },
  }))));
});

test("G3c setup requests and results are exact and path-safe", () => {
  const create = parseRequestText(JSON.stringify(request({
    operation: "project.create",
    payload: { name: "PBMC bare replicate" },
  })));
  assert.doesNotThrow(() => completedResponse(create, {
    project_id: "project-created",
    verified: true,
    composer_empty: true,
    user_turn_count: 0,
    root_frame_id: null,
    root_state: null,
  }, 10));

  const freshChat = parseRequestText(JSON.stringify(request({
    operation: "chat.new",
    payload: { project_id: "project-created" },
  })));
  const blank = chatObservation({
    project_id: "project-created",
    chat_id: "chat-created",
    transcript: [],
    user_turn_count: 0,
    root_frame_id: null,
    response_control_id: null,
    current_turn_state: "indeterminate",
  });
  assert.doesNotThrow(() => completedResponse(freshChat, blank, 10));
  assert.throws(
    () => completedResponse(freshChat, { ...blank, user_turn_count: 1 }, 10),
    (error) => error instanceof BoundaryError && error.code === "INVALID_RESPONSE",
  );

  const upload = parseRequestText(JSON.stringify(request({
    operation: "attachment.upload",
    payload: {
      project_id: "project-created",
      chat_id: "chat-created",
      source_path: "/private/tmp/pbmc_tiny_seed.csv",
    },
  })));
  const accepted = {
    project_id: "project-created",
    chat_id: "chat-created",
    filename: "pbmc_tiny_seed.csv",
    accepted: true,
  };
  assert.deepEqual(completedResponse(upload, accepted, 10).result, accepted);
  assert.throws(
    () => parseRequestText(JSON.stringify(request({
      operation: "attachment.upload",
      payload: {
        project_id: "project-created",
        chat_id: "chat-created",
        source_path: "relative.csv",
      },
    }))),
    (error) => error instanceof BoundaryError && error.code === "INVALID_TEXT",
  );
  assert.throws(
    () => completedResponse(upload, { ...accepted, source_path: "/private/tmp/file" }, 10),
    (error) => error instanceof BoundaryError && error.code === "INVALID_FIELDS",
  );
});

test("G3b turn results distinguish continuation from settlement", () => {
  const parsed = parseRequestText(JSON.stringify(request({
    operation: "turn.wait",
    payload: {
      project_id: "project-001",
      chat_id: "chat-001",
      continuation: continuation(),
    },
  })));
  assert.doesNotThrow(() => completedResponse(parsed, turnResult(), 10));
  const settled = turnResult({
    turn_state: "settled",
    settled: {
      stop_hidden: true,
      stable_samples: 2,
      new_response_control_id: "turn-assistant-new",
    },
    continuation: null,
  });
  assert.doesNotThrow(() => completedResponse(parsed, settled, 10));
  for (const invalid of [
    turnResult({ continuation: null }),
    turnResult({ turn_state: "settled", continuation: null }),
    turnResult({
      continuation: continuation({ delivery_text_sha256: "c".repeat(64) }),
    }),
  ]) {
    assert.throws(
      () => completedResponse(parsed, invalid, 10),
      (error) => error instanceof BoundaryError,
    );
  }
  const mismatched = turnResult({
    delivery: {
      ...turnResult().delivery,
      authored_prompt_sha256: "c".repeat(64),
    },
    continuation: continuation({ authored_prompt_sha256: "c".repeat(64) }),
  });
  assert.throws(
    () => completedResponse(parsed, mismatched, 10),
    (error) => error instanceof BoundaryError && error.code === "INVALID_RESPONSE",
  );
});

test("approval resolution result requires verified clearance", () => {
  const parsed = parseRequestText(JSON.stringify(request({
    operation: "approval.resolve",
    payload: {
      project_id: "project-001",
      chat_id: "chat-001",
      root_frame_id: "root-001",
      card_id: "approval:abc:0",
      decision: "deny",
      expected_fingerprint: "c".repeat(64),
    },
  })));
  const result = {
    project_id: "project-001",
    chat_id: "chat-001",
    root_frame_id: "root-001",
    card_id: "approval:abc:0",
    decision: "deny",
    verified_cleared: true,
  };
  assert.doesNotThrow(() => completedResponse(parsed, result, 2));
  assert.throws(
    () => completedResponse(parsed, { ...result, verified_cleared: false }, 2),
    (error) => error instanceof BoundaryError,
  );
});

test("project observations support rooted and rootless projects", () => {
  const parsed = parseRequestText(JSON.stringify(request({
    operation: "project.inspect",
    payload: { project_id: "project-001" },
  })));
  const rooted = {
    project_id: "project-001",
    verified: true,
    composer_empty: true,
    user_turn_count: 1,
    root_frame_id: "root-001",
    root_state: "completed",
  };
  assert.deepEqual(completedResponse(parsed, rooted, 2).result, rooted);
  assert.doesNotThrow(() => completedResponse(parsed, {
    ...rooted,
    user_turn_count: 0,
    root_frame_id: null,
    root_state: null,
  }, 2));
  assert.throws(
    () => completedResponse(parsed, { ...rooted, root_state: null }, 2),
    (error) => error instanceof BoundaryError && error.code === "INVALID_RESPONSE",
  );
});

test("chat observations support blank and rooted chats", () => {
  const parsed = parseRequestText(JSON.stringify(request({
    operation: "chat.inspect",
    payload: { project_id: "project-001", chat_id: "chat-001" },
  })));
  assert.deepEqual(
    completedResponse(parsed, chatObservation(), 2).result,
    chatObservation(),
  );
  const blank = chatObservation({
    transcript: [],
    user_turn_count: 0,
    root_frame_id: null,
    response_control_id: null,
  });
  assert.deepEqual(completedResponse(parsed, blank, 2).result, blank);
});

test("chat transcript roles IDs counts and response control fail closed", () => {
  const parsed = parseRequestText(JSON.stringify(request({
    operation: "chat.inspect",
    payload: { project_id: "project-001", chat_id: "chat-001" },
  })));
  for (const value of [
    chatObservation({
      transcript: [
        { turn_id: "turn-user", role: "tool", text: "x", truncated: false },
      ],
      user_turn_count: 0,
      response_control_id: null,
    }),
    chatObservation({ user_turn_count: 2 }),
    chatObservation({ response_control_id: "turn-missing" }),
    chatObservation({ response_control_id: "turn-user" }),
    chatObservation({
      transcript: [
        { turn_id: "turn-duplicate", role: "user", text: "x", truncated: false },
        {
          turn_id: "turn-duplicate",
          role: "assistant",
          text: "y",
          truncated: false,
        },
      ],
      response_control_id: "turn-duplicate",
    }),
  ]) {
    assert.throws(
      () => completedResponse(parsed, value, 2),
      (error) => error instanceof BoundaryError,
    );
  }
});

test("chat transcript text is byte-bounded with explicit truncation", () => {
  const parsed = parseRequestText(JSON.stringify(request({
    operation: "chat.inspect",
    payload: { project_id: "project-001", chat_id: "chat-001" },
  })));
  const bounded = "x".repeat(16384);
  assert.doesNotThrow(() => completedResponse(parsed, chatObservation({
    transcript: [
      { turn_id: "turn-user", role: "user", text: bounded, truncated: true },
    ],
    response_control_id: null,
  }), 2));
  assert.throws(
    () => completedResponse(parsed, chatObservation({
      transcript: [
        {
          turn_id: "turn-user",
          role: "user",
          text: "x".repeat(16385),
          truncated: false,
        },
      ],
      response_control_id: null,
    }), 2),
    (error) => error instanceof BoundaryError && error.code === "INVALID_TEXT",
  );
});

test("approval observations must agree with turn state", () => {
  const parsed = parseRequestText(JSON.stringify(request({
    operation: "chat.inspect",
    payload: { project_id: "project-001", chat_id: "chat-001" },
  })));
  const card = {
    card_id: "approval:abc:0",
    fingerprint: "a".repeat(64),
    title: "Permission required",
    kind: "approval",
  };
  assert.doesNotThrow(() => completedResponse(parsed, chatObservation({
    current_turn_state: "approval_required",
    approval_cards: [card],
  }), 2));
  assert.throws(
    () => completedResponse(parsed, chatObservation({ approval_cards: [card] }), 2),
    (error) => error instanceof BoundaryError && error.code === "INVALID_RESPONSE",
  );
});

test("context observations are exact sorted and hashed", () => {
  const parsed = parseRequestText(JSON.stringify(request({
    operation: "agent_context.inspect",
    payload: { project_id: "project-001" },
  })));
  const observation = {
    project_id: "project-001",
    enabled_skills: ["Audit", "Lineage"],
    context_hash: "b".repeat(64),
  };
  assert.deepEqual(completedResponse(parsed, observation, 2).result, observation);
  assert.doesNotThrow(() => completedResponse(parsed, {
    ...observation,
    enabled_skills: [],
  }, 2));
  assert.throws(
    () => completedResponse(parsed, {
      ...observation,
      enabled_skills: ["Lineage", "Audit"],
    }, 2),
    (error) => error instanceof BoundaryError && error.code === "INVALID_RESPONSE",
  );
  assert.throws(
    () => completedResponse(parsed, { ...observation, extra: true }, 2),
    (error) => error instanceof BoundaryError && error.code === "INVALID_FIELDS",
  );
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
