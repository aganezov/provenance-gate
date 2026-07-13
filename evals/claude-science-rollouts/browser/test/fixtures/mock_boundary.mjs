#!/usr/bin/env node

import { fileURLToPath } from "node:url";

import { runMain } from "../../src/main.mjs";
import { BoundaryError } from "../../src/protocol.mjs";
import { deliveryTextSha256, sha256Hex } from "../../src/turns.mjs";

function delivery(payload, rootFrameId, turnId = "turn-user-new") {
  return {
    root_frame_id: rootFrameId,
    authored_prompt_sha256: payload.authored_prompt_sha256,
    delivery_text_sha256: deliveryTextSha256(payload.prompt),
    normalized_user_turn_id: turnId,
  };
}

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
  "project.inspect": async (payload) => ({
    project_id: payload.project_id,
    verified: true,
    composer_empty: true,
    user_turn_count: 1,
    root_frame_id: "root-001",
    root_state: "completed",
  }),
  "project.create": async () => ({
    project_id: "project-created",
    verified: true,
    composer_empty: true,
    user_turn_count: 0,
    root_frame_id: null,
    root_state: null,
  }),
  "attachment.upload": async (payload) => ({
    project_id: payload.project_id,
    chat_id: payload.chat_id,
    filename: payload.source_path.split("/").at(-1),
    accepted: true,
  }),
  "chat.new": async (payload) => ({
    project_id: payload.project_id,
    chat_id: "chat-created",
    transcript: [],
    user_turn_count: 0,
    composer_empty: true,
    root_frame_id: null,
    response_control_id: null,
    current_turn_state: "indeterminate",
    approval_cards: [],
  }),
  "model.select": async (payload) => ({
    project_id: payload.project_id,
    chat_id: payload.chat_id,
    model_label: payload.model_label,
    previous_model_label: "Research Default",
    changed: payload.model_label !== "Research Default",
    confirmed: true,
  }),
  "chat.inspect": async (payload) => ({
    project_id: payload.project_id,
    chat_id: payload.chat_id,
    transcript: payload.root_frame_id
      ? [
          { turn_id: "turn-user", role: "user", text: "Question", truncated: false },
          {
            turn_id: "turn-assistant",
            role: "assistant",
            text: "Answer",
            truncated: false,
          },
        ]
      : [],
    user_turn_count: payload.root_frame_id ? 1 : 0,
    composer_empty: true,
    root_frame_id: payload.root_frame_id ?? null,
    response_control_id: payload.root_frame_id ? "turn-assistant" : null,
    current_turn_state: "indeterminate",
    approval_cards: [],
  }),
  "agent_context.inspect": async (payload) => ({
    project_id: payload.project_id,
    enabled_skills: [],
    context_hash: "a".repeat(64),
  }),
  "turn.submit_wait": async (payload) => {
    if (sha256Hex(payload.prompt) !== payload.authored_prompt_sha256) {
      throw new BoundaryError("PROMPT_HASH_MISMATCH", "Prompt hash does not match");
    }
    const rootFrameId = payload.root_frame_id ?? "root-created";
    const proof = delivery(payload, rootFrameId);
    if (payload.prompt === "needs wait") {
      return {
        project_id: payload.project_id,
        chat_id: payload.chat_id,
        root_frame_id: rootFrameId,
        turn_state: "busy",
        root_created: payload.root_mode === "new",
        delivery: proof,
        settled: null,
        approval: null,
        continuation: {
          project_id: payload.project_id,
          chat_id: payload.chat_id,
          root_frame_id: rootFrameId,
          authored_prompt_sha256: payload.authored_prompt_sha256,
          delivery_text_sha256: proof.delivery_text_sha256,
          normalized_user_turn_id: proof.normalized_user_turn_id,
          baseline_response_control_id: "turn-assistant-old",
        },
      };
    }
    return {
      project_id: payload.project_id,
      chat_id: payload.chat_id,
      root_frame_id: rootFrameId,
      turn_state: "settled",
      root_created: payload.root_mode === "new",
      delivery: proof,
      settled: {
        stop_hidden: true,
        stable_samples: 2,
        new_response_control_id: "turn-assistant-new",
      },
      approval: null,
      continuation: null,
    };
  },
  "turn.wait": async (payload) => ({
    project_id: payload.project_id,
    chat_id: payload.chat_id,
    root_frame_id: payload.continuation.root_frame_id,
    turn_state: "settled",
    root_created: false,
    delivery: {
      root_frame_id: payload.continuation.root_frame_id,
      authored_prompt_sha256: payload.continuation.authored_prompt_sha256,
      delivery_text_sha256: payload.continuation.delivery_text_sha256,
      normalized_user_turn_id: payload.continuation.normalized_user_turn_id,
    },
    settled: {
      stop_hidden: true,
      stable_samples: 2,
      new_response_control_id: "turn-assistant-new",
    },
    approval: null,
    continuation: null,
  }),
  "approval.resolve": async (payload) => ({
    project_id: payload.project_id,
    chat_id: payload.chat_id,
    root_frame_id: payload.root_frame_id,
    card_id: payload.card_id,
    decision: payload.decision,
    verified_cleared: true,
  }),
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
