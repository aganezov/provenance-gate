import assert from "node:assert/strict";
import test from "node:test";

import {
  buildCreateProjectSource,
  buildNewChatSource,
  buildOpenAttachmentChooserSource,
  buildVerifyAttachmentSource,
} from "../src/setup.mjs";

const origin = "http://127.0.0.1:8875";

test("G3c generated setup sources are bounded and syntactically valid", () => {
  const create = buildCreateProjectSource({
    origin,
    name: "PBMC bare replicate",
  });
  const newChat = buildNewChatSource({ origin, projectId: "project-001" });
  const chooser = buildOpenAttachmentChooserSource({
    origin,
    projectId: "project-001",
    chatId: "chat-001",
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

test("attachment browser code receives only the basename", () => {
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
