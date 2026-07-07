---
name: drive-claude-science
description: >-
  Drive Claude Science (a.k.a. CS or "operon" — the local research app at
  http://127.0.0.1:8765, NOT Claude Code) from the browser, like an automated
  researcher: spin up a project, submit prompts into the CS chat, clear approval
  ("Allow") gates, wait through variable-length agent runs, and iterate across
  multiple turns — efficiently and reliably. Use this whenever you need to create or
  open a CS project/session, type/submit a prompt in the CS UI, run a task in CS, or
  automate a multi-step CS flow in the browser. This skill ONLY drives the CS browser
  UI — it does NOT read the CS database or compute provenance/DAG/cones/checksums
  (that lives in the surrounding app). Trigger on any mention of Claude Science,
  operon, "the CS UI", 127.0.0.1:8765 / localhost:8765, or driving / automating /
  submitting prompts to CS — even if the request never says the word "skill".
---

# Driving Claude Science (CS)

CS ("operon") is a local app that runs Claude on your data in the browser. It is a
**separate app from Claude Code** that happens to share Claude primitives. Web UI at
**http://127.0.0.1:8765**.

**This skill is a browser driver, and only that.** It automates the CS UI the way a
researcher would — create a project, submit prompts, clear "Allow" gates, wait out
agent runs, iterate across turns. It **does not read the CS database** and **does not
compute any provenance / DAG / cone / checksums** — that belongs to the surrounding
app, which reads `operon-cli.db` itself. Keeping the skill UI-only is deliberate: one
job, done reliably.

**Golden rules:**
1. **`127.0.0.1`, never `localhost`.** CS binds IPv4-only; `localhost`→`::1` refuses.
2. **Text/DOM reads over screenshots** — screenshot only to show a human. A DOM read is
   ~1 KB; a screenshot is 100 KB+ and slows every turn.
3. **CS agent runs vary wildly in length** (seconds to minutes — ~12 s to ~215 s
   observed for trivial tasks). Never assume a fixed wait; drive completion with the
   re-invokable settle poll (Step 5).

Full access recipe and every gotcha with its fix: [`RECIPE.md`](RECIPE.md). The
in-page building blocks referenced below live in
[`scripts/cs_drive.js`](scripts/cs_drive.js); auth in
[`scripts/cs_auth.sh`](scripts/cs_auth.sh).

---

## Workflow

Use the **claude-in-chrome** MCP. Load its tools with ONE ToolSearch call if they're
deferred (see the MCP instructions). The flow is deterministic up front, then a bounded
settle poll absorbs each variable agent run.

### Step 1 — Connect to the browser that can reach CS
`list_connected_browsers` → **AskUserQuestion** to let the user pick (the protocol
requires a human choice here) → `select_browser`. Pick the **personal** profile; a
proxied/managed profile fails CS with `ERR_CONNECTION_REFUSED`.

### Step 2 — Authenticate (daemon-scoped magic link)
```bash
scripts/cs_auth.sh          # prints a single-use, ~3-min login URL, host = 127.0.0.1
```
`navigate` the tab to that URL, then click the **"Sign in"** button (a still-valid
cached session may skip straight to the projects list — that's fine). Generate the link
immediately before navigating — it expires fast, and a daemon restart expires any login.

### Step 3 — Create / open a project
Paste **Block A** of `scripts/cs_drive.js` into `javascript_tool` (substitute the
project name). It clicks *New project*, sets the name via React's native setter, clicks
*Create*, and waits for the `/projects/` nav — all in one call. Returns `{ok, url}`.

### Step 4 — Submit a prompt
Paste **Block B**. On the current build the composer is a **light-DOM** contenteditable
and the verified submit is: locate it → `execCommand insertText` (React registers it) →
click the **exact "Send"** button that materializes once there's text — no coordinate
click, no Enter key. (Block B also pierces shadow DOM for older builds and returns the
composer's `center` for a precise coordinate fallback.)
- `{submitted:true}` → run started, done.
- `{found:true, inserted:true, submitted:false, center}` → MCP `key Return` (the composer
  is focused), or coordinate-click `center` → `type` → `key Return`.
- `{found:false}` → run **Block D** to re-find the composer (a build changed it).

### Step 5 — Wait for the run (re-invokable settle poll)
Paste **Block C**. It loops *inside the page*, **auto-clicks the "Allow" approval gate**
the instant it appears, and treats the *Stop* button as the busy signal. It is **bounded
to ~35 s** because the `javascript_tool` eval hard-times-out near 45 s (CS runs routinely
exceed that):
- `{settled:true}` → run done.
- `{stillRunning:true}` → it hit the cap mid-run — **paste Block C again** until settled.

A short run settles in one call; a long run (minutes) takes several — that's expected and
cheap, and far fewer turns than checking every few seconds. `artifactsSeen` reports the
filenames that appeared **on screen** — enough to confirm the run produced something; the
surrounding app reads the DB for the authoritative result.

### Step 6 — Iterate (multi-turn)
For a multi-step flow, **repeat Steps 4–5**: a follow-up prompt goes into the same
composer and continues the same session. Verified across turns with wildly different run
lengths (~12 s to ~215 s) and a mid-run approval gate — the driver survives it.

If a submit path breaks on a new CS build, paste **Block D** (diagnose): it reports the
editable candidates and action buttons with their rects and labels so you can re-aim
without a screenshot.

---

## The core technique (why this is fast)

The cost floor is **~1 LLM-turn per browser interaction**, and the two things that
actually slow a driving session are (a) screenshot payloads and (b) waiting on CS's own
agent. So: **batch the deterministic prefix** (create → submit are fixed steps), use
text/DOM reads over screenshots, and **collapse each variable-length agent run into the
bounded settle poll** (Block C) — one in-page call for a short run, a handful for a long
one — instead of a read-and-check turn every few seconds. A real run showed ~30 s of CS
agent time can otherwise carry ~130 s of driving overhead — almost all of it avoidable.

See [`RECIPE.md`](RECIPE.md) for the access details and the full gotcha list with fixes.
