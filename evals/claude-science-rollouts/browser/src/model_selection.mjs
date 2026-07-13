const MAX_MODEL_LABEL_BYTES = 128;

function byteLength(value) {
  return new TextEncoder().encode(value).length;
}

export function validateModelLabel(value) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.trim() !== value ||
    byteLength(value) > MAX_MODEL_LABEL_BYTES ||
    /[\u0000-\u001f\u007f]/.test(value)
  ) {
    throw new TypeError("model label is invalid");
  }
  return value;
}

export function normalizeVisibleText(value) {
  return String(value).trim().replace(/\s+/g, " ");
}

export function primaryModelOptionLabel(option) {
  const explicit = normalizeVisibleText(option.ariaLabel ?? "");
  if (explicit) return explicit;
  const lines = String(option.text ?? "")
    .split(/\r?\n/u)
    .map(normalizeVisibleText)
    .filter(Boolean);
  return lines[0] ?? "";
}

export function matchModelOption(options, targetLabel) {
  const target = validateModelLabel(targetLabel);
  const matches = options
    .map((option, index) => ({
      index,
      text: normalizeVisibleText(option.text),
      primaryLabel: primaryModelOptionLabel(option),
      checked: option.checked === true,
      topLevel: option.topLevel !== false,
    }))
    .filter(({ primaryLabel, topLevel }) => topLevel && primaryLabel === target);
  if (matches.length !== 1) {
    throw new Error("MODEL_OPTION_AMBIGUOUS");
  }
  const { topLevel: _discard, ...match } = matches[0];
  return match;
}

export function buildSelectModelSource({
  origin,
  projectId,
  chatId,
  modelLabel,
  helpersSource,
}) {
  const target = validateModelLabel(modelLabel);
  if (
    ![origin, projectId, chatId, helpersSource].every(
      (value) => typeof value === "string" && value,
    )
  ) {
    throw new TypeError("model selection source arguments must be non-empty strings");
  }
  return `async (page) => {
    let mutationAttempted = false;
    const originOf = (value) =>
      String(value).match(/^https?:[/][/][^/?#]+/)?.[0] ?? null;
    const fail = (code) => {
      const error = new Error(code);
      error.boundaryCode = code;
      throw error;
    };
    const normalize = (value) => String(value).trim().replace(/\\s+/g, " ");
    const selectedLabel = async () => {
      const controls = page.locator('button[aria-label^="Model: "]:visible');
      if (await controls.count() !== 1) fail("MODEL_CONTROL_AMBIGUOUS");
      const accessible = await controls.getAttribute("aria-label");
      if (!accessible?.startsWith("Model: ")) fail("MODEL_CONTROL_AMBIGUOUS");
      const label = accessible.slice("Model: ".length).trim();
      if (!label) fail("MODEL_CONTROL_AMBIGUOUS");
      return { control: controls, label };
    };
    try {
      const identity = await page.evaluate(() => {
        const { activeChatId, composerState, parseLocation, transcript } = (${helpersSource})();
        return {
          ...parseLocation(),
          chatId: activeChatId(),
          composer: composerState(),
          turns: transcript(16384),
        };
      });
      if (identity.origin !== ${JSON.stringify(origin)}) fail("NAVIGATION_DRIFT");
      if (
        identity.projectId !== ${JSON.stringify(projectId)} ||
        identity.chatId !== ${JSON.stringify(chatId)} ||
        identity.rootFrameId !== null ||
        identity.composer?.empty !== true ||
        !Array.isArray(identity.turns) ||
        identity.turns.length !== 0
      ) fail("MODEL_SELECTION_REQUIRES_BLANK_CHAT");

      const before = await selectedLabel();
      if (before.label === ${JSON.stringify(target)}) {
        return {
          _origin: identity.origin,
          _mutation_attempted: false,
          result: {
            project_id: ${JSON.stringify(projectId)},
            chat_id: ${JSON.stringify(chatId)},
            model_label: before.label,
            previous_model_label: before.label,
            changed: false,
            confirmed: true,
          },
        };
      }

      await before.control.click();
      const visibleMenuOptions = page.locator(
        '[role="menu"]:visible [role="menuitemradio"]:visible',
      );
      await visibleMenuOptions.first().waitFor({ state: "visible", timeout: 5000 })
        .catch(() => fail("MODEL_OPTION_AMBIGUOUS"));
      const visibleMenus = page.locator('[role="menu"]:visible');
      const menuCount = await visibleMenus.count();
      if (menuCount < 1 || menuCount > 20) fail("MODEL_OPTION_AMBIGUOUS");
      const modelMenus = [];
      for (let menuIndex = 0; menuIndex < menuCount; menuIndex += 1) {
        const menu = visibleMenus.nth(menuIndex);
        const menuHandle = await menu.elementHandle();
        const candidates = menu.locator('[role="menuitemradio"]:visible');
        const topLevel = [];
        for (let optionIndex = 0; optionIndex < await candidates.count(); optionIndex += 1) {
          const option = candidates.nth(optionIndex);
          const selectable = await option.evaluate((node, menuNode) =>
            node.closest('[role="menu"]') === menuNode &&
            node.parentElement?.closest('[role="menuitemradio"]') === null,
          menuHandle);
          if (selectable) topLevel.push(option);
        }
        if (topLevel.length > 0) modelMenus.push(topLevel);
      }
      if (modelMenus.length !== 1 || modelMenus[0].length > 20) {
        fail("MODEL_OPTION_AMBIGUOUS");
      }
      const matches = [];
      for (const option of modelMenus[0]) {
          const primaryLabel = await option.evaluate((node) => {
            const explicit = String(node.getAttribute('aria-label') ?? "")
            .trim().replace(/\\s+/g, " ");
          if (explicit) return explicit;
          return String(node.innerText ?? "").split(/\\r?\\n/)
            .map((line) => line.trim().replace(/\\s+/g, " "))
            .find(Boolean) ?? "";
        });
        if (primaryLabel === ${JSON.stringify(target)}) matches.push(option);
      }
      if (matches.length !== 1) fail("MODEL_OPTION_AMBIGUOUS");

      mutationAttempted = true;
      await matches[0].click();
      const confirmationDeadline = Date.now() + 5000;
      let after;
      while (true) {
        after = await selectedLabel();
        if (after.label === ${JSON.stringify(target)}) break;
        if (Date.now() >= confirmationDeadline) fail("MODEL_SELECTION_UNCONFIRMED");
        await page.waitForTimeout(50);
      }
      return {
        _origin: identity.origin,
        _mutation_attempted: true,
        result: {
          project_id: ${JSON.stringify(projectId)},
          chat_id: ${JSON.stringify(chatId)},
          model_label: after.label,
          previous_model_label: before.label,
          changed: true,
          confirmed: true,
        },
      };
    } catch (error) {
      return {
        _origin: originOf(page.url()),
        _mutation_attempted: mutationAttempted,
        _boundary_error: error?.boundaryCode ?? "MODEL_SELECTION_UNCONFIRMED",
      };
    }
  }`;
}
