const MAX_TURN_TEXT_BYTES = 16384;

// The scheme://host origin of an http(s) URL — IPv6-bracket aware — or "" when the URL has none.
// One definition, injected into every browser-source builder, so the origin echoed on error paths
// is computed identically everywhere (the boundary compares it to the expected session origin).
export function originFromHttpUrl(value) {
  if (typeof value !== "string") throw new TypeError("Page URL must be a string");
  const match = value.match(/^(https?):\/\/(\[[^\]]+\]|[^/?#]+)(?=\/|[?#]|$)/u);
  return match ? `${match[1]}://${match[2]}` : "";
}

export const ORIGIN_FROM_HTTP_URL_SOURCE = originFromHttpUrl.toString();

export function classifyObservedTurnState({
  busy,
  approvalCardCount,
  inputRequired,
  failed,
}) {
  if (busy) return "busy";
  if (approvalCardCount > 0) return "approval_required";
  if (inputRequired) return "input_required";
  if (failed) return "failed";
  return "indeterminate";
}

export function approvalControlKind(value) {
  if (typeof value !== "string") return null;
  const normalized = value.replace(/\s+/gu, " ").trim().toLowerCase();
  const compact = normalized.replace(/\s+/gu, "");
  if (
    /^(approve|allow)(?:\s|$)/u.test(normalized) ||
    /^allow(?:forchat)?forthisconversation$/u.test(compact)
  ) {
    return "allow";
  }
  if (/^(deny|reject)(?:\s|$)/u.test(normalized)) return "deny";
  return null;
}

export function approvalCardTitle(value) {
  if (typeof value !== "string") return "";
  return value
    .split(/\r?\n/gu)
    .map((line) => line.replace(/\s+/gu, " ").trim())
    .find(Boolean)
    ?.slice(0, 512) ?? "";
}

function pageObservationHelpers(classifyApprovalControl, extractApprovalTitle) {
  const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;

  const isVisible = (element) => {
    if (!(element instanceof Element)) return false;
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return (
      style.display !== "none" &&
      style.visibility !== "hidden" &&
      rect.width > 0 &&
      rect.height > 0
    );
  };

  const visibleElements = (root, selector) =>
    Array.from(root.querySelectorAll(selector)).filter(isVisible);

  const parseLocation = () => {
    const project = location.pathname.match(/\/projects\/([^/?#]+)/);
    const root = location.pathname.match(/\/frames\/([^/?#]+)/);
    return {
      origin: location.origin,
      projectId: project?.[1] ?? null,
      rootFrameId: root?.[1] ?? null,
    };
  };

  const findReactObject = (
    element,
    predicate,
    { traverseReturn = false } = {},
  ) => {
    const queue = [];
    const seen = new Set();
    for (const key of Object.keys(element)) {
      if (key.startsWith("__reactFiber$") || key.startsWith("__reactProps$")) {
        queue.push({ value: element[key], depth: 0 });
      }
    }
    let visited = 0;
    while (queue.length && visited < 2500) {
      const { value, depth } = queue.shift();
      visited += 1;
      if (!value || typeof value !== "object" || seen.has(value)) continue;
      seen.add(value);
      if (predicate(value)) return value;
      if (depth >= 7) continue;
      for (const [key, child] of Object.entries(value)) {
        if (
          ["alternate", "sibling", "stateNode", "_owner"].includes(key) ||
          (key === "return" && !traverseReturn)
        ) {
          continue;
        }
        if (child && typeof child === "object") {
          queue.push({ value: child, depth: depth + 1 });
        }
      }
    }
    return null;
  };

  const activeChatId = () => {
    const panes = visibleElements(document, '[data-testid="session-tabs-pane"]');
    if (panes.length !== 1) return null;
    const model = findReactObject(
      panes[0],
      (value) =>
        Object.hasOwn(value, "activeTabId") &&
        typeof value.activeTabId === "string",
    );
    return IDENTIFIER.test(model?.activeTabId ?? "")
      ? model.activeTabId
      : null;
  };

  const rootFrameModel = (rootFrameId) => {
    if (rootFrameId === null) return null;
    const matches = [];
    for (const item of visibleElements(
      document,
      '[data-testid="session-rail-item"]',
    )) {
      const frame = findReactObject(
        item,
        (value) =>
          value.id === rootFrameId &&
          value.root_frame_id === rootFrameId &&
          typeof value.project_id === "string",
      );
      if (frame) matches.push(frame);
    }
    return matches.length === 1 ? matches[0] : null;
  };

  const composerState = () => {
    const composers = visibleElements(document, '[data-testid="composer"]');
    if (composers.length !== 1) return null;
    const editables = visibleElements(
      composers[0],
      '[role="textbox"],[contenteditable="true"]',
    );
    if (editables.length !== 1) return null;
    return {
      empty: (editables[0].textContent ?? "").trim().length === 0,
      visible: true,
    };
  };

  const controlVisible = (root, label) =>
    visibleElements(root, `button[aria-label="${label}"]`).length > 0;

  const textFromVisibleNodes = (root) => {
    const parts = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const parent = node.parentElement;
      if (!parent || !isVisible(parent)) continue;
      if (
        parent.closest(
          'button,[role="button"],nav,[aria-hidden="true"],[data-testid="turn-artifact-tray"]',
        )
      ) {
        continue;
      }
      const value = (node.textContent ?? "").replace(/\s+/g, " ").trim();
      if (value) parts.push(value);
    }
    return parts.join(" ").replace(/\s+/g, " ").trim();
  };

  const truncateUtf8 = (value, maximumBytes) => {
    const encoder = new TextEncoder();
    const bytes = encoder.encode(value);
    if (bytes.length <= maximumBytes) {
      return { text: value, truncated: false };
    }
    for (let end = maximumBytes; end > 0; end -= 1) {
      try {
        const text = new TextDecoder("utf-8", { fatal: true }).decode(
          bytes.slice(0, end),
        );
        return { text, truncated: true };
      } catch {}
    }
    return { text: "", truncated: true };
  };

  const turnIdentifier = (element, role) => {
    const label = role === "user" ? "Edit message" : "Copy to clipboard";
    const controls = visibleElements(
      element,
      `button[aria-label="${label}"]`,
    );
    if (controls.length !== 1) return null;
    const field = role === "user" ? "_uuid" : "messageUuid";
    const model = findReactObject(
      controls[0],
      (value) =>
        Object.hasOwn(value, field) &&
        typeof value[field] === "string" &&
        IDENTIFIER.test(value[field]),
      { traverseReturn: true },
    );
    return model?.[field] ?? null;
  };

  const transcript = (maximumTextBytes) => {
    const scrolls = visibleElements(
      document,
      '[data-testid="conversation-scroll"]',
    );
    if (scrolls.length === 0) return [];
    if (scrolls.length !== 1) return null;
    const indexed = visibleElements(scrolls[0], "[data-index]")
      .filter((element) => element.parentElement !== null)
      .map((element) => ({
        element,
        index: Number.parseInt(element.getAttribute("data-index") ?? "", 10),
      }))
      .filter((item) => Number.isSafeInteger(item.index));
    const byIndex = new Map();
    for (const item of indexed) {
      if (!byIndex.has(item.index)) byIndex.set(item.index, item.element);
    }
    const turns = [];
    for (const [index, element] of [...byIndex.entries()].sort(
      ([left], [right]) => left - right,
    )) {
      const user = controlVisible(element, "Edit message");
      const assistant = controlVisible(element, "Copy to clipboard");
      if (user === assistant) continue;
      const role = user ? "user" : "assistant";
      const turnId = turnIdentifier(element, role);
      if (!turnId) return null;
      const bounded = truncateUtf8(
        textFromVisibleNodes(element),
        maximumTextBytes,
      );
      turns.push({
        index,
        turn_id: turnId,
        role,
        text: bounded.text,
        truncated: bounded.truncated,
      });
    }
    const ids = new Set(turns.map((turn) => turn.turn_id));
    return ids.size === turns.length ? turns : null;
  };

  const sha256 = async (value) => {
    const bytes = new TextEncoder().encode(value);
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest))
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
  };

  const approvalCards = async () => {
    const cardElements = visibleElements(document, '[data-testid="approval-card"]');
    const cards = [];
    for (const [index, card] of cardElements.entries()) {
      const controlKinds = visibleElements(card, "button").map((control) =>
        classifyApprovalControl(
          control.getAttribute("aria-label") ?? control.textContent ?? "",
        ));
      const title = extractApprovalTitle(card.innerText ?? card.textContent ?? "");
      // Surface only an actionable card: a live allow AND deny control (CS may render more than one
      // allow variant, e.g. "Allow" plus "Allow for this conversation" — the resolver picks the one
      // it wants) with a title. A resolved or historical card that keeps the testid but has no live
      // pair is skipped, not treated as malformed state that would poison the whole observation.
      if (
        !controlKinds.includes("allow") ||
        !controlKinds.includes("deny") ||
        !title
      ) {
        continue;
      }
      const fingerprint = await sha256(`approval\n${title}`);
      cards.push({
        card_id: `approval:${fingerprint.slice(0, 32)}:${index}`,
        fingerprint,
        title,
        kind: "approval",
      });
    }
    return cards;
  };

  const turnStateSignals = () => {
    const main = document.querySelector('main,[role="main"]') ?? document.body;
    const inputRequired = visibleElements(
      main,
      'button,[role="textbox"]',
    ).some((element) =>
      /^(Answer|Reply|Provide input|Submit answer|Custom answer)(?:\s|$)/i.test(
        element.getAttribute("aria-label") ??
          element.getAttribute("placeholder") ??
          element.textContent ??
          "",
      ),
    );
    return {
      busy: controlVisible(document, "Stop"),
      inputRequired,
      failed: visibleElements(main, '[role="alert"]').length > 0,
    };
  };

  return {
    activeChatId,
    approvalCards,
    composerState,
    parseLocation,
    rootFrameModel,
    sha256,
    transcript,
    turnStateSignals,
    visibleElements,
  };
}

export const PAGE_OBSERVATION_HELPERS_SOURCE = `() => (
  ${pageObservationHelpers.toString()}
)(
  ${approvalControlKind.toString()},
  ${approvalCardTitle.toString()}
)`;
const HELPERS = PAGE_OBSERVATION_HELPERS_SOURCE;
const CLASSIFY_TURN_STATE = classifyObservedTurnState.toString();

export function buildProjectInspectSource(projectId) {
  return `async (page) => {
    const expectedProjectId = ${JSON.stringify(projectId)};
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
    const frame = locationState.rootFrameId
      ? await page.evaluate((rootFrameId) => {
          const { rootFrameModel } = (${HELPERS})();
          return rootFrameModel(rootFrameId);
        }, locationState.rootFrameId)
      : null;
    return {
      _origin: locationState.origin,
      project_id: locationState.projectId,
      verified: locationState.projectId === expectedProjectId,
      composer_empty: composer?.empty ?? null,
      user_turn_count: Array.isArray(turns)
        ? turns.filter((turn) => turn.role === "user").length
        : null,
      root_frame_id: locationState.rootFrameId,
      root_state: frame?.status ?? null,
      _root_project_id: frame?.project_id ?? null,
    };
  }`;
}

export function buildChatInspectSource(projectId, chatId, rootFrameId) {
  return `async (page) => {
    const expectedProjectId = ${JSON.stringify(projectId)};
    const expectedChatId = ${JSON.stringify(chatId)};
    const expectedRootFrameId = ${JSON.stringify(rootFrameId)};
    const locationState = await page.evaluate(() => {
      const { parseLocation } = (${HELPERS})();
      return parseLocation();
    });
    const observedChatId = await page.evaluate(() => {
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
    const cards = await page.evaluate(async () => {
      const { approvalCards } = (${HELPERS})();
      return approvalCards();
    });
    const stateSignals = await page.evaluate(() => {
      const { turnStateSignals } = (${HELPERS})();
      return turnStateSignals();
    });
    const frame = locationState.rootFrameId
      ? await page.evaluate((observedRootFrameId) => {
          const { rootFrameModel } = (${HELPERS})();
          return rootFrameModel(observedRootFrameId);
        }, locationState.rootFrameId)
      : null;
    const publicTurns = Array.isArray(turns)
      ? turns.map(({ index: _discardIndex, ...turn }) => turn)
      : turns;
    const assistantTurns = Array.isArray(publicTurns)
      ? publicTurns.filter((turn) => turn.role === "assistant")
      : [];
    const responseControlId = assistantTurns.length > 0
      ? assistantTurns[assistantTurns.length - 1].turn_id
      : null;
    const state = Array.isArray(publicTurns) && composer && Array.isArray(cards)
      ? (${CLASSIFY_TURN_STATE})({
          ...stateSignals,
          approvalCardCount: cards.length,
          composerVisible: composer.visible,
          userTurnCount: publicTurns.filter((turn) => turn.role === "user").length,
          assistantTurnCount: assistantTurns.length,
          responseControlId,
        })
      : null;
    return {
      _origin: locationState.origin,
      project_id: locationState.projectId,
      chat_id: observedChatId,
      transcript: publicTurns,
      user_turn_count: Array.isArray(publicTurns)
        ? publicTurns.filter((turn) => turn.role === "user").length
        : null,
      composer_empty: composer?.empty ?? null,
      root_frame_id: locationState.rootFrameId,
      response_control_id: responseControlId,
      current_turn_state: state,
      approval_cards: cards,
      _root_project_id: frame?.project_id ?? null,
      _expected: {
        project_id: expectedProjectId,
        chat_id: expectedChatId,
        root_frame_id: expectedRootFrameId,
      },
    };
  }`;
}

export function buildContextInspectSource(projectId) {
  return `async (page) => {
    const expectedProjectId = ${JSON.stringify(projectId)};
    const projectSettingsHeading = page.getByRole("heading", {
      name: "Project Settings",
      exact: true,
    });
    const globalSettingsHeading = page.getByRole("heading", {
      name: "Settings",
      exact: true,
    });
    const contextTextbox = () => page.getByRole("textbox", {
      name: /^e\\.g\\., This project studies/,
    });
    let projectSettingsOpen = false;
    let capabilitiesOpen = false;
    try {
      const before = await page.evaluate(() => {
        const { parseLocation } = (${HELPERS})();
        return parseLocation();
      });
      if (before.projectId !== expectedProjectId) {
        return { _origin: before.origin, project_id: before.projectId };
      }
      if (await globalSettingsHeading.isVisible().catch(() => false)) {
        await page.getByRole("button", {
          name: "Close settings",
          exact: true,
        }).click();
      }
      const back = page.getByRole("button", {
        name: "Back to dashboard",
        exact: true,
      });
      await back.waitFor({ state: "visible", timeout: 15000 });
      const triggers = back.locator("..").getByRole("button");
      const menus = [];
      for (let index = 0; index < await triggers.count(); index += 1) {
        const trigger = triggers.nth(index);
        if (
          await trigger.isVisible().catch(() => false) &&
          await trigger.getAttribute("aria-haspopup") === "menu"
        ) {
          menus.push(trigger);
        }
      }
      if (menus.length !== 1) throw new Error("Ambiguous project menu");
      await menus[0].click();
      await page.getByRole("menuitem", {
        name: "Project settings",
        exact: true,
      }).click();
      await projectSettingsHeading.waitFor({ state: "visible", timeout: 15000 });
      projectSettingsOpen = true;
      await contextTextbox().waitFor({ state: "visible", timeout: 15000 });
      const rawContext = await contextTextbox().inputValue();
      const normalizedContext = rawContext
        .replace(/\\r\\n/g, "\\n")
        .replace(/\\n$/, "");
      const contextHash = await page.evaluate(async (value) => {
        const { sha256 } = (${HELPERS})();
        return sha256(value);
      }, normalizedContext);
      await page.getByRole("button", { name: "Cancel", exact: true }).click();
      projectSettingsOpen = false;

      await page.getByTestId("capabilities-button").click();
      capabilitiesOpen = true;
      await page.getByTestId("settings-tab-skills").click();
      await page.getByRole("heading", { name: "Skills", exact: true }).waitFor({
        state: "visible",
        timeout: 15000,
      });
      const enabledSkills = await page.evaluate(() => {
        const { visibleElements } = (${HELPERS})();
        const controls = visibleElements(document, '[role="switch"]');
        const names = [];
        let observedSkillCards = 0;
        for (const control of controls) {
          const card = control.closest('[role="button"]');
          if (!card || card.getAttribute("aria-expanded") !== null) continue;
          observedSkillCards += 1;
          if (control.getAttribute("aria-checked") !== "true") continue;
          const name = (card.textContent ?? "").replace(/\\s+/g, " ").trim();
          if (!name) return null;
          names.push(name.slice(0, 256));
        }
        if (observedSkillCards === 0) return null;
        const unique = [...new Set(names)].sort();
        return unique.length === names.length ? unique : null;
      });
      await page.getByRole("button", {
        name: "Close settings",
        exact: true,
      }).click();
      capabilitiesOpen = false;
      const after = await page.evaluate(() => {
        const { parseLocation } = (${HELPERS})();
        return parseLocation();
      });
      return {
        _origin: after.origin,
        project_id: after.projectId,
        enabled_skills: enabledSkills,
        context_hash: contextHash,
      };
    } finally {
      if (projectSettingsOpen) {
        await page.getByRole("button", { name: "Cancel", exact: true })
          .click().catch(() => {});
      }
      if (capabilitiesOpen) {
        await page.getByRole("button", {
          name: "Close settings",
          exact: true,
        }).click().catch(() => {});
      }
    }
  }`;
}
