import assert from "node:assert/strict";
import test from "node:test";

import {
  buildCreateProjectSource,
  buildNewChatSource,
  buildSetAttachmentSource,
  buildVerifyAttachmentSource,
} from "../src/setup.mjs";

const origin = "http://127.0.0.1:8875";

test("G3c generated setup sources are bounded and syntactically valid", () => {
  const create = buildCreateProjectSource({
    origin,
    name: "PBMC bare replicate",
  });
  const newChat = buildNewChatSource({ origin, projectId: "project-001" });
  const chooser = buildSetAttachmentSource({
    origin,
    projectId: "project-001",
    chatId: "chat-001",
    sourcePath: "/private/tmp/pbmc_tiny_seed.csv",
  });
  const verify = buildVerifyAttachmentSource({
    origin,
    projectId: "project-001",
    chatId: "chat-001",
    filename: "pbmc_tiny_seed.csv",
  });
  for (const source of [create, newChat, chooser, verify]) {
    assert.doesNotThrow(() => new Function(`return (${source})`));
    assert.doesNotMatch(source, /new URL/);
  }
  assert.equal(create.match(/await create\.click\(\)/g)?.length, 1);
  assert.equal(newChat.match(/await control\.click\(\)/g)?.length, 1);
  assert.match(verify, /Preview pbmc_tiny_seed\.csv/);
});

test("attachment verification receives only the basename", () => {
  const sourcePath = "/private/tmp/private-parent/pbmc_tiny_seed.csv";
  const verify = buildVerifyAttachmentSource({
    origin,
    projectId: "project-001",
    chatId: "chat-001",
    filename: "pbmc_tiny_seed.csv",
  });
  assert.doesNotMatch(verify, new RegExp(sourcePath));
  assert.doesNotMatch(verify, /source_path/);
});

test("attachment input waits for exact rootless chat hydration before one selection", async () => {
  const source = buildSetAttachmentSource({
    origin,
    projectId: "project-001",
    chatId: "chat-001",
    sourcePath: "/private/tmp/pbmc_tiny_seed.csv",
  });
  let observations = 0;
  let selectedPath = null;
  const page = {
    async evaluate() {
      observations += 1;
      return observations === 1
        ? {
            origin,
            projectId: "project-001",
            rootFrameId: null,
            chatId: null,
            composer: null,
            turns: null,
          }
        : {
            origin,
            projectId: "project-001",
            rootFrameId: null,
            chatId: "chat-001",
            composer: { empty: true, visible: true },
            turns: [],
          };
    },
    locator(selector) {
      assert.equal(selector, 'input[type="file"]');
      return {
        count: async () => 1,
        async setInputFiles(path) { selectedPath = path; },
      };
    },
    async waitForTimeout() {},
    url: () => `${origin}/projects/project-001`,
  };
  const run = new Function(`return (${source})`)();

  const result = await run(page);

  assert.deepEqual(result, {
    _origin: origin,
    _mutation_attempted: true,
    result: { ready: true, chat_id: "chat-001" },
  });
  assert.equal(observations, 2);
  assert.equal(selectedPath, "/private/tmp/pbmc_tiny_seed.csv");
});

test("attachment input reports the exact pre-upload control stage", async () => {
  const source = buildSetAttachmentSource({
    origin,
    projectId: "project-001",
    chatId: "chat-provisional",
    sourcePath: "/private/tmp/pbmc_tiny_seed.csv",
  });
  const page = {
    async evaluate() {
      return {
        origin,
        projectId: "project-001",
        rootFrameId: null,
        chatId: "chat-provisional",
        composer: { empty: true, visible: true },
        turns: [],
      };
    },
    locator(selector) {
      assert.equal(selector, 'input[type="file"]');
      return {
        count: async () => 0,
      };
    },
    async waitForTimeout() {},
    url: () => `${origin}/projects/project-001`,
  };
  const run = new Function(`return (${source})`)();

  const result = await run(page);

  assert.equal(result._origin, origin);
  assert.equal(result._mutation_attempted, false);
  assert.equal(result._boundary_error, "ATTACHMENT_INPUT_UNAVAILABLE");
});


test("attachment waits for the requested chat and never uploads to another", async () => {
  const source = buildSetAttachmentSource({
    origin,
    projectId: "project-001",
    chatId: "chat-requested",
    sourcePath: "/private/tmp/pbmc_tiny_seed.csv",
  });
  // a different blank chat is active first; only once the requested chat is active does it upload.
  const chatIds = ["chat-other", "chat-other", "chat-requested"];
  let index = 0;
  let uploadedTo = null;
  const page = {
    async evaluate() {
      const chatId = chatIds[Math.min(index++, chatIds.length - 1)];
      return {
        origin,
        projectId: "project-001",
        rootFrameId: null,
        chatId,
        composer: { empty: true, visible: true },
        turns: [],
      };
    },
    locator() {
      return {
        count: async () => 1,
        setInputFiles: async () => { uploadedTo = "chat-requested"; },
      };
    },
    async waitForTimeout() {},
    url: () => `${origin}/projects/project-001`,
  };
  const run = new Function(`return (${source})`)();

  const result = await run(page);

  assert.equal(result._mutation_attempted, true);
  assert.equal(result.result.chat_id, "chat-requested");
  assert.equal(uploadedTo, "chat-requested");
});

test("new chat returns a distinct post-click identity after a rooted chat", async () => {
  const source = buildNewChatSource({ origin, projectId: "project-001" });
  let explicitSession = false;
  let postClickObservations = 0;
  let clicks = 0;
  let waits = 0;
  const page = {
    async evaluate(fn) {
      const body = fn.toString();
      if (body.includes("return parseLocation();")) {
        if (explicitSession) postClickObservations += 1;
        return {
          origin,
          projectId: "project-001",
          rootFrameId: explicitSession ? null : "root-existing",
        };
      }
      if (body.includes("return activeChatId();")) {
        return explicitSession && postClickObservations >= 2
          ? "chat-explicit"
          : "chat-implicit";
      }
      if (body.includes("return composerState();")) {
        return { empty: true, visible: true };
      }
      if (body.includes("return transcript(maximumTextBytes);")) return [];
      assert.fail(`Unexpected page.evaluate callback: ${body.slice(0, 80)}`);
    },
    getByTestId(value) {
      assert.equal(value, "new-session-button");
      return {
        waitFor: async () => {},
        count: async () => 1,
        async click() {
          clicks += 1;
          explicitSession = true;
        },
      };
    },
    waitForURL: async () => {},
    async waitForTimeout() { waits += 1; },
    url: () => `${origin}/projects/project-001`,
  };
  const run = new Function(`return (${source})`)();

  const result = await run(page);

  assert.equal(clicks, 1);
  assert.equal(postClickObservations, 2);
  assert.equal(waits, 1);
  assert.equal(result._mutation_attempted, true);
  assert.equal(result.result.chat_id, "chat-explicit");
  assert.equal(result.result.user_turn_count, 0);
});
