/* ===========================================================================
 * cs_drive.js — in-page building blocks for driving Claude Science in-browser.
 *
 * These are NOT a library you import. Each block below is a self-contained
 * async IIFE meant to be pasted into ONE `mcp__claude-in-chrome__javascript_tool`
 * call. Replace the __PLACEHOLDERS__, keep the leading `await`, run it, read the
 * small object it returns.
 *
 * Three hard rules, learned the hard way (see RECIPE.md), each with a WHY:
 *  1. ALWAYS keep the leading `await`. An unawaited async IIFE returns `{}` while
 *     its side effects still run — you'll think it failed when it didn't.
 *  2. Return SMALL structured objects. A large innerText read comes back as
 *     "[BLOCKED: Cookie/query string data]". Never return page text in bulk.
 *  3. NEVER loop in-page longer than ~35s. The CDP eval behind javascript_tool
 *     hard-times-out around 45s; a longer loop fails the tool call even though the
 *     page keeps running. Block C is bounded and designed to be re-invoked.
 *
 * Division of labor that makes these reliable (verified on build 0.1.16-dev):
 *  - Buttons (New project / Create / Stop / Allow / Send / artifact chips) live in
 *    the LIGHT DOM  -> plain document.querySelectorAll('button') reaches them.
 *  - The composer is a contenteditable rich editor. In current builds it is in the
 *    LIGHT DOM (a <div role="textbox" contenteditable>); older builds encapsulated
 *    it in shadow DOM. The deepAll() walk below finds it either way.
 * ===========================================================================*/


/* ---------------------------------------------------------------------------
 * BLOCK A — CREATE A PROJECT in one call. (Verified.)
 * Clicks "New project", fills the name via React's native setter, clicks
 * "Create", and waits for the /projects/ navigation. Returns {ok,url}.
 * ------------------------------------------------------------------------- */
await (async () => {
  const NAME = "__PROJECT_NAME__";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const byText = (re) =>
    [...document.querySelectorAll("button")].find((b) =>
      re.test((b.textContent || "").trim())
    );

  const open = byText(/new project/i);
  if (!open) return { ok: false, reason: "no 'New project' button" };
  open.click();

  let input = null;
  for (let i = 0; i < 40 && !input; i++) {
    input = document.querySelector('input[placeholder="Project name"]');
    if (!input) await sleep(100);
  }
  if (!input) return { ok: false, reason: "name input never appeared" };

  // React controls the input's value; a raw .value= is ignored. Use the native
  // setter, then dispatch 'input' so React's onChange sees it.
  const setValue = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    "value"
  ).set;
  setValue.call(input, NAME);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  await sleep(60);

  const create = byText(/^create$/i) || byText(/create project/i);
  if (!create) return { ok: false, reason: "no 'Create' button" };
  create.click();

  for (let i = 0; i < 60; i++) {
    if (location.pathname.includes("/projects/"))
      return { ok: true, url: location.href };
    await sleep(100);
  }
  return { ok: false, reason: "no /projects/ nav", url: location.href };
})();


/* ---------------------------------------------------------------------------
 * BLOCK B — SUBMIT A PROMPT. (Verified fully-programmatic path.)
 * The verified flow on build 0.1.16-dev: locate the contenteditable composer,
 * insert the text with execCommand (which React registers), then click the
 * "Send" button that MATERIALIZES once there is text. No coordinate click, no
 * Enter-guessing needed. It still returns the composer's `center` so that if a
 * future build hides the Send button you can fall back to a PRECISE coordinate
 * click (MCP click center -> type -> key Return) instead of a hardcoded pixel.
 *
 * Read the return value:
 *   {submitted:true}                          -> run started, you're done.
 *   {found:true, inserted:true, submitted:false, center:{x,y}}
 *        -> text is in the box but no Send fired. Submit with MCP `key Return`
 *           (composer already focused), or coordinate-click center then type+Return.
 *   {found:true, inserted:false, center:{x,y}} -> coordinate-click center, then
 *           MCP `type` the text, then `key Return`.
 *   {found:false}                              -> screenshot to orient; the
 *           composer selector changed — run Block D to re-find it.
 * ------------------------------------------------------------------------- */
await (async () => {
  const TEXT = "__PROMPT__";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // BFS across the light DOM AND every shadow root (superset: works whether the
  // composer is light-DOM, as in current builds, or shadow-encapsulated).
  const deepAll = (sel) => {
    const out = [];
    const roots = [document];
    while (roots.length) {
      const r = roots.shift();
      r.querySelectorAll(sel).forEach((e) => out.push(e));
      r.querySelectorAll("*").forEach((e) => {
        if (e.shadowRoot) roots.push(e.shadowRoot);
      });
    }
    return out;
  };
  const visible = (e) => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const label = (b) =>
    (b.getAttribute("aria-label") || b.getAttribute("title") || b.textContent || "").trim();

  const cands = deepAll(
    '[contenteditable="true"], textarea, [role="textbox"]'
  ).filter(visible);
  const el = cands[cands.length - 1]; // the composer is typically the last editable
  if (!el)
    return { found: false, reason: "no editable composer (light or shadow DOM)" };

  const rect = el.getBoundingClientRect();
  const center = {
    x: Math.round(rect.left + rect.width / 2),
    y: Math.round(rect.top + rect.height / 2),
  };

  // insert text
  el.focus();
  let inserted = false;
  const tag = el.tagName;
  if (tag === "TEXTAREA" || tag === "INPUT") {
    const proto =
      tag === "TEXTAREA" ? window.HTMLTextAreaElement : window.HTMLInputElement;
    Object.getOwnPropertyDescriptor(proto.prototype, "value").set.call(el, TEXT);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    inserted = el.value === TEXT;
  } else {
    // contenteditable: execCommand insertText is verified to register in CS's
    // editor; fall back to a synthetic beforeinput if a build stops honoring it.
    document.execCommand("insertText", false, TEXT);
    inserted = (el.textContent || "").includes(TEXT);
    if (!inserted) {
      el.dispatchEvent(
        new InputEvent("beforeinput", {
          bubbles: true,
          cancelable: true,
          inputType: "insertText",
          data: TEXT,
        })
      );
      inserted = (el.textContent || "").includes(TEXT);
    }
  }
  await sleep(120); // let the Send button appear now that there's text

  // submit: the EXACT "Send" button (never the "More send options" menu button)
  let submitted = false,
    sentVia = null;
  if (inserted) {
    const btns = [...document.querySelectorAll("button")];
    const send =
      btns.find((b) => /^send$/i.test(label(b)) && !b.disabled) ||
      btns.find(
        (b) => /send|submit/i.test(label(b)) && !/more/i.test(label(b)) && !b.disabled
      );
    if (send) {
      send.click();
      sentVia = "send-button";
      await sleep(500);
      // run started == a Stop button exists OR the URL advanced to a frame
      submitted =
        [...document.querySelectorAll("button")].some(
          (b) =>
            /^stop$/i.test((b.textContent || "").trim()) ||
            /stop/i.test(b.getAttribute("aria-label") || "")
        ) || location.href.includes("/frames/");
    }
  }

  return {
    found: true,
    inserted,
    submitted,
    sentVia,
    center, // <- coordinate-click fallback aims here
    editorPreview: (el.value ?? el.textContent ?? "").slice(0, 60),
    hint: submitted
      ? "run started"
      : inserted
      ? "text set but no Send fired — MCP `key Return`, or coordinate-click center then type+Return"
      : "insert failed — coordinate-click center, then MCP type + key Return",
  };
})();


/* ---------------------------------------------------------------------------
 * BLOCK C — SETTLE POLL (bounded, re-invokable). The single biggest speed lever.
 * A CS agent run is variable-length and routinely exceeds the ~45s CDP eval
 * limit, so this loop is CAPPED at ~35s per call. It auto-clicks the "Allow"
 * approval gate the instant it appears, treats the "Stop" button as the busy
 * signal, and returns a tiny status object.
 *
 *   {settled:true}      -> the run is done; read results from the DB.
 *   {stillRunning:true} -> the ~35s cap hit while the run was busy: CALL THIS
 *                          BLOCK AGAIN (it resumes watching). Repeat until settled.
 *
 * A run shorter than ~35s settles in ONE call. Longer runs take a few calls —
 * still far fewer turns than read-then-check polling. The DB (cs_provenance.py)
 * is the AUTHORITATIVE read of what was produced; `artifactsSeen` is a convenience.
 * ------------------------------------------------------------------------- */
await (async () => {
  const CALL_CAP_MS = 35000; // MUST stay < the ~45s CDP eval timeout
  const IDLE_MS = 2500; // "settled" = this long with no Stop button and no gate
  const GRACE_MS = 3000; // don't declare done before the run has a chance to start

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const buttons = () => [...document.querySelectorAll("button")];
  const textIs = (re) =>
    buttons().filter((b) => re.test((b.textContent || "").trim()));
  const stopPresent = () =>
    textIs(/^stop$/i).length > 0 ||
    buttons().some((b) => /stop/i.test(b.getAttribute("aria-label") || ""));

  const t0 = Date.now();
  let approvals = 0,
    sawStop = false,
    lastBusy = Date.now(),
    settled = false;

  while (Date.now() - t0 < CALL_CAP_MS) {
    // Approval gate: click Allow / Allow globally the moment it shows.
    const allow =
      textIs(/^allow( globally)?$/i)[0] ||
      buttons().find((b) => /^allow/i.test((b.textContent || "").trim()));
    if (allow) {
      allow.click();
      approvals++;
      lastBusy = Date.now();
      await sleep(300);
      continue;
    }
    if (stopPresent()) {
      sawStop = true;
      lastBusy = Date.now();
      await sleep(700);
      continue;
    }
    // No Stop, no gate. Wait out the grace + idle windows before calling it done.
    if (!sawStop && Date.now() - t0 < GRACE_MS) {
      await sleep(400);
      continue;
    }
    if (Date.now() - lastBusy >= IDLE_MS) {
      settled = true;
      break;
    }
    await sleep(400);
  }

  // Best-effort artifact filenames: only elements whose ENTIRE text is a filename
  // (keeps the return small and dodges the large-innerText block).
  const artifactsSeen = [
    ...new Set(
      [...document.querySelectorAll("*")]
        .map((e) => (e.textContent || "").trim())
        .filter((t) =>
          /^[\w.\-]{1,60}\.(csv|tsv|txt|json|png|jpe?g|parquet|py|md|html?|pdf|xlsx?|bam|vcf)$/i.test(
            t
          )
        )
    ),
  ].slice(0, 25);

  return {
    settled,
    stillRunning: !settled,
    elapsedMs: Date.now() - t0,
    sawStop,
    approvals,
    artifactsSeen,
  };
})();


/* ---------------------------------------------------------------------------
 * BLOCK D — DIAGNOSE (use once when hardening / when a submit path breaks).
 * Reports the editable candidates and likely action buttons with their rects and
 * labels, so you can see exactly what the composer looks like in the current
 * build without paying for a screenshot. Returns a small structured summary.
 * ------------------------------------------------------------------------- */
await (async () => {
  const deepAll = (sel) => {
    const out = [];
    const roots = [document];
    while (roots.length) {
      const r = roots.shift();
      r.querySelectorAll(sel).forEach((e) => out.push(e));
      r.querySelectorAll("*").forEach((e) => {
        if (e.shadowRoot) roots.push(e.shadowRoot);
      });
    }
    return out;
  };
  const box = (e) => {
    const r = e.getBoundingClientRect();
    return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
  };
  const editors = deepAll('[contenteditable="true"], textarea, [role="textbox"]')
    .map((e) => ({
      tag: e.tagName,
      role: e.getAttribute("role"),
      inShadow: e.getRootNode() !== document,
      box: box(e),
    }))
    .filter((e) => e.box.w > 0);
  const actionButtons = [...document.querySelectorAll("button")]
    .map((b) => ({
      label: (
        b.getAttribute("aria-label") || b.getAttribute("title") || b.textContent || ""
      )
        .trim()
        .slice(0, 24),
      disabled: b.disabled,
      box: box(b),
    }))
    .filter((b) => b.box.w > 0 && b.box.y > 400) // bottom-of-page action row
    .slice(0, 15);
  return { viewport: { w: innerWidth, h: innerHeight }, editors, actionButtons };
})();
