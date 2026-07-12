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
  "project.inspect": async (payload) => ({
    project_id: payload.project_id,
    verified: true,
    composer_empty: true,
    user_turn_count: 1,
    root_frame_id: "root-001",
    root_state: "completed",
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
    enabled_skills: ["Audit skill"],
    context_hash: "a".repeat(64),
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
