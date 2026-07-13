import { PAGE_OBSERVATION_HELPERS_SOURCE } from "./observations.mjs";

const HELPERS = PAGE_OBSERVATION_HELPERS_SOURCE;
const MAX_TURN_TEXT_BYTES = 16384;

export function buildCreateProjectSource({ origin, name }) {
  return `async (page) => {
    let mutationAttempted = false;
    const originOf = (value) =>
      String(value).match(/^https?:[/][/][^/?#]+/)?.[0] ?? null;
    const fail = (code) => {
      const error = new Error(code);
      error.boundaryCode = code;
      throw error;
    };
    try {
      await page.goto(${JSON.stringify(origin)});
      if (originOf(page.url()) !== ${JSON.stringify(origin)}) {
        fail("NAVIGATION_DRIFT");
      }
      const newProject = page.getByRole("button", {
        name: "New project",
        exact: true,
      });
      await newProject.waitFor({ state: "visible", timeout: 15000 });
      if (await newProject.count() !== 1) fail("MALFORMED_BROWSER_STATE");
      await newProject.click();
      const nameInput = page.getByRole("textbox", {
        name: "Project name",
        exact: true,
      });
      await nameInput.waitFor({ state: "visible", timeout: 15000 });
      if (await nameInput.count() !== 1) fail("MALFORMED_BROWSER_STATE");
      await nameInput.fill(${JSON.stringify(name)});
      const create = page.getByRole("button", { name: "Create", exact: true });
      await create.waitFor({ state: "visible", timeout: 15000 });
      if (await create.count() !== 1) fail("MALFORMED_BROWSER_STATE");
      mutationAttempted = true;
      await create.click();
      await page.waitForURL(/\\/projects\\/[^/?#]+\\/?$/, { timeout: 15000 });

      const locationState = await page.evaluate(() => {
        const { parseLocation } = (${HELPERS})();
        return parseLocation();
      });
      const composer = await page.evaluate(() => {
        const { composerState } = (${HELPERS})();
        return composerState();
      });
      const turns = await page.evaluate((maximumTextBytes) => {
        const { transcript } = (${HELPERS})();
        return transcript(maximumTextBytes);
      }, ${MAX_TURN_TEXT_BYTES});
      if (
        locationState.origin !== ${JSON.stringify(origin)} ||
        locationState.projectId === null ||
        locationState.rootFrameId !== null ||
        composer?.empty !== true ||
        !Array.isArray(turns) ||
        turns.filter((turn) => turn.role === "user").length !== 0
      ) {
        fail("FRESH_PROJECT_NOT_VERIFIED");
      }
      return {
        _origin: locationState.origin,
        _mutation_attempted: true,
        result: {
          project_id: locationState.projectId,
          verified: true,
          composer_empty: true,
          user_turn_count: 0,
          root_frame_id: null,
          root_state: null,
        },
      };
    } catch (error) {
      return {
        _origin: originOf(page.url()),
        _mutation_attempted: mutationAttempted,
        _boundary_error: error?.boundaryCode ?? "MALFORMED_BROWSER_STATE",
      };
    }
  }`;
}

export function buildNewChatSource({ origin, projectId }) {
  return `async (page) => {
    let mutationAttempted = false;
    const originOf = (value) =>
      String(value).match(/^https?:[/][/][^/?#]+/)?.[0] ?? null;
    const fail = (code) => {
      const error = new Error(code);
      error.boundaryCode = code;
      throw error;
    };
    const collect = async () => {
      const locationState = await page.evaluate(() => {
        const { parseLocation } = (${HELPERS})();
        return parseLocation();
      });
      const chatId = await page.evaluate(() => {
        const { activeChatId } = (${HELPERS})();
        return activeChatId();
      });
      const composer = await page.evaluate(() => {
        const { composerState } = (${HELPERS})();
        return composerState();
      });
      const turns = await page.evaluate((maximumTextBytes) => {
        const { transcript } = (${HELPERS})();
        return transcript(maximumTextBytes);
      }, ${MAX_TURN_TEXT_BYTES});
      return { locationState, chatId, composer, turns };
    };
    const verifyBlank = (observation) => {
      if (observation.locationState.origin !== ${JSON.stringify(origin)}) {
        fail("NAVIGATION_DRIFT");
      }
      if (
        observation.locationState.projectId !== ${JSON.stringify(projectId)} ||
        observation.locationState.rootFrameId !== null ||
        observation.chatId === null ||
        observation.composer?.empty !== true ||
        !Array.isArray(observation.turns) ||
        observation.turns.length !== 0
      ) {
        fail("BLANK_CHAT_NOT_VERIFIED");
      }
    };
    try {
      let observation = await collect();
      if (observation.locationState.origin !== ${JSON.stringify(origin)}) {
        fail("NAVIGATION_DRIFT");
      }
      if (observation.locationState.projectId !== ${JSON.stringify(projectId)}) {
        fail("IDENTITY_MISMATCH");
      }
      const alreadyBlank =
        observation.locationState.rootFrameId === null &&
        observation.chatId !== null &&
        observation.composer?.empty === true &&
        Array.isArray(observation.turns) &&
        observation.turns.length === 0;
      if (!alreadyBlank) {
        const implicitChatId = observation.chatId;
        const control = page.getByTestId("new-session-button");
        await control.waitFor({ state: "visible", timeout: 15000 });
        if (await control.count() !== 1) fail("MALFORMED_BROWSER_STATE");
        mutationAttempted = true;
        await control.click();
        await page.waitForURL(
          (url) =>
            url.origin === ${JSON.stringify(origin)} &&
            url.pathname.replace(/\\/$/, "") ===
              "/projects/" + ${JSON.stringify(projectId)},
          { timeout: 15000 },
        );
        const explicitDeadline = Date.now() + 15000;
        while (true) {
          observation = await collect();
          if (observation.locationState.origin !== ${JSON.stringify(origin)}) {
            fail("NAVIGATION_DRIFT");
          }
          if (
            observation.locationState.projectId !== ${JSON.stringify(projectId)} ||
            observation.locationState.rootFrameId !== null ||
            (observation.composer !== null && observation.composer.empty !== true) ||
            (Array.isArray(observation.turns) && observation.turns.length !== 0)
          ) fail("BLANK_CHAT_NOT_VERIFIED");
          const explicitIdentity =
            observation.chatId !== null &&
            (implicitChatId === null || observation.chatId !== implicitChatId);
          const blankHydrated =
            observation.composer?.empty === true &&
            Array.isArray(observation.turns) &&
            observation.turns.length === 0;
          if (explicitIdentity && blankHydrated) break;
          if (Date.now() >= explicitDeadline) fail("BLANK_CHAT_NOT_VERIFIED");
          await page.waitForTimeout(100);
        }
      }
      verifyBlank(observation);
      return {
        _origin: observation.locationState.origin,
        _mutation_attempted: mutationAttempted,
        result: {
          project_id: ${JSON.stringify(projectId)},
          chat_id: observation.chatId,
          transcript: [],
          user_turn_count: 0,
          composer_empty: true,
          root_frame_id: null,
          response_control_id: null,
          current_turn_state: "indeterminate",
          approval_cards: [],
        },
      };
    } catch (error) {
      return {
        _origin: originOf(page.url()),
        _mutation_attempted: mutationAttempted,
        _boundary_error: error?.boundaryCode ?? "MALFORMED_BROWSER_STATE",
      };
    }
  }`;
}

export function buildSetAttachmentSource({
  origin,
  projectId,
  chatId,
  sourcePath,
}) {
  return `async (page) => {
    const originOf = (value) =>
      String(value).match(/^https?:[/][/][^/?#]+/)?.[0] ?? null;
    const fail = (code) => {
      const error = new Error(code);
      error.boundaryCode = code;
      throw error;
    };
    let stage = "draft";
    let mutationAttempted = false;
    try {
      const readinessDeadline = Date.now() + 15000;
      let identity;
      while (true) {
        identity = await page.evaluate(() => {
          const { activeChatId, composerState, parseLocation, transcript } = (${HELPERS})();
          return {
            ...parseLocation(),
            chatId: activeChatId(),
            composer: composerState(),
            turns: transcript(${MAX_TURN_TEXT_BYTES}),
          };
        });
        if (identity.origin !== ${JSON.stringify(origin)}) fail("NAVIGATION_DRIFT");
        if (
          identity.projectId !== ${JSON.stringify(projectId)} ||
          identity.rootFrameId !== null ||
          (identity.composer !== null && identity.composer.empty !== true) ||
          (Array.isArray(identity.turns) && identity.turns.length !== 0)
        ) fail("IDENTITY_MISMATCH");
        if (
          identity.chatId !== null &&
          identity.composer?.empty === true &&
          Array.isArray(identity.turns) &&
          identity.turns.length === 0
        ) break;
        if (Date.now() >= readinessDeadline) fail("ATTACHMENT_DRAFT_NOT_READY");
        await page.waitForTimeout(100);
      }
      stage = "file_input";
      const input = page.locator('input[type="file"]');
      if (await input.count() !== 1) fail("ATTACHMENT_INPUT_UNAVAILABLE");
      mutationAttempted = true;
      await input.setInputFiles(${JSON.stringify(sourcePath)});
      return {
        _origin: identity.origin,
        _mutation_attempted: true,
        result: { ready: true, chat_id: identity.chatId },
      };
    } catch (error) {
      const stageCode = {
        draft: "ATTACHMENT_DRAFT_NOT_READY",
        file_input: "ATTACHMENT_INPUT_UNAVAILABLE",
      }[stage];
      return {
        _origin: originOf(page.url()),
        _mutation_attempted: mutationAttempted,
        _boundary_error: error?.boundaryCode ?? stageCode,
      };
    }
  }`;
}

export function buildVerifyAttachmentSource({ origin, projectId, chatId, filename }) {
  return `async (page) => {
    const originOf = (value) =>
      String(value).match(/^https?:[/][/][^/?#]+/)?.[0] ?? null;
    const fail = (code) => {
      const error = new Error(code);
      error.boundaryCode = code;
      throw error;
    };
    try {
      const identity = await page.evaluate(() => {
        const { activeChatId, parseLocation } = (${HELPERS})();
        return { ...parseLocation(), chatId: activeChatId() };
      });
      if (identity.origin !== ${JSON.stringify(origin)}) fail("NAVIGATION_DRIFT");
      if (
        identity.projectId !== ${JSON.stringify(projectId)} ||
        identity.chatId !== ${JSON.stringify(chatId)} ||
        identity.rootFrameId !== null
      ) fail("IDENTITY_MISMATCH");
      const add = page.getByRole("button", {
        name: "Add to message",
        exact: true,
      });
      await add.waitFor({ state: "visible", timeout: 15000 });
      const surface = add.locator("xpath=ancestor::*[.//*[@role='textbox']][1]");
      const previews = surface.getByTitle(
        ${JSON.stringify(`Preview ${filename}`)},
        { exact: true },
      );
      const deadline = Date.now() + 15000;
      let visible = 0;
      while (Date.now() <= deadline) {
        visible = 0;
        for (let index = 0; index < await previews.count(); index += 1) {
          if (await previews.nth(index).isVisible()) visible += 1;
        }
        if (visible === 1) break;
        if (visible > 1) fail("AMBIGUOUS_ATTACHMENT");
        await page.waitForTimeout(100);
      }
      if (visible !== 1) fail("ATTACHMENT_NOT_VERIFIED");
      return {
        _origin: identity.origin,
        result: {
          project_id: ${JSON.stringify(projectId)},
          chat_id: ${JSON.stringify(chatId)},
          filename: ${JSON.stringify(filename)},
          accepted: true,
        },
      };
    } catch (error) {
      return {
        _origin: originOf(page.url()),
        _boundary_error: error?.boundaryCode ?? "MALFORMED_BROWSER_STATE",
      };
    }
  }`;
}
