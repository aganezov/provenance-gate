---
name: drive-claude-science
description: >-
  Drive Claude Science (a.k.a. CS or "operon" — the local research app at
  http://127.0.0.1:8765, NOT Claude Code) fast and reliably. Use this whenever you
  need to spin up or run a CS project/session, submit a prompt into the CS chat,
  wait through a CS agent run, click past CS approval ("Allow") gates, OR read a CS
  run's provenance — its artifacts, sha256 checksums, the consumes-DAG / "cone",
  execution cells, or faithfulness pins — whether live in the browser for a demo or
  straight from the read-only SQLite store for fast machine reads. Trigger on any
  mention of Claude Science, operon, "the CS UI", 127.0.0.1:8765 / localhost:8765,
  operon-cli.db, automating or driving CS, or reading CS artifacts/provenance —
  even if the request never says the word "skill".
---

# Driving Claude Science (CS)

CS ("operon") is a local app that runs Claude on your data in the browser. It is a
**separate app from Claude Code** that happens to share Claude primitives. Web UI at
**http://127.0.0.1:8765**; per-org state in a SQLite store (`operon-cli.db`).

There are **two ways in, and they are not interchangeable:**

| You want to…                                   | Use            | Why                                                  |
|------------------------------------------------|----------------|------------------------------------------------------|
| **Read** a run's provenance / artifacts / cone | **DB mode**    | No browser, no LLM-turn-per-click. Fast + exact.     |
| **Drive** CS (create, prompt, run) / demo live | **Browser mode** | Only the UI can *act*; the store is read-only.     |

**Three golden rules:**
1. **`127.0.0.1`, never `localhost`.** CS binds IPv4-only; `localhost`→`::1` refuses.
2. **Read via the DB, drive via the browser, screenshot only to show a human.** A
   screenshot is 100 KB+ and slows every turn; a DOM/DB read is 1–2 KB.
3. **The DB is the source of truth for what a run produced** — never scrape results
   out of the UI when you can read them from `operon-cli.db`.

For the full access recipe and every gotcha with its fix, read
[`RECIPE.md`](RECIPE.md). The two sections below are the workflow.

---

## Mode 1 — Read provenance (fast, the default)

`scripts/cs_provenance.py` opens the store **read-only** (`file:…?mode=ro`, so it
never blocks or mutates the running daemon) and prints the provenance primitives.
It discovers the store automatically from `~/.claude-science/active-org.json`.

```bash
python3 scripts/cs_provenance.py projects              # list projects
python3 scripts/cs_provenance.py show <name-or-id>     # full read-back for a project
python3 scripts/cs_provenance.py cone <version-id>     # transitive cone of one version
python3 scripts/cs_provenance.py verify <name-or-id>   # just the faithfulness pins
```

`show` gives you the whole picture in one call: the frame tree (a CS session =
**UPLOADS + OPERON compute + REVIEWER advisory**), every artifact with its versions
and a **faithfulness pin** (on-disk sha256 recomputed vs. the recorded checksum →
`PIN✓`/`PIN✗`), the labeled **consumes DAG**, the execution cells, and CS's advisory
verification checks. `cone` adds the deterministic **conflict** (≥2 versions of one
artifact in the cone) and **currency** (a non-latest version) signals. Add `--json`
to any command for machine-readable output.

This is also the blueprint for any headless reader of CS (e.g. a provenance gate):
the store *is* the substrate. There is **no CLI verb to run a task headlessly** — the
`claude-science`/`operon` binary only exposes `serve/open/url/status/logs/stop/…`.
So: **drive in the browser, read from the DB.**

---

## Mode 2 — Drive in the browser (for the live demo)

Use the **claude-in-chrome** MCP. Load its tools with ONE ToolSearch call if they're
deferred (see the MCP instructions). The flow is deterministic up front, then a bounded
settle poll absorbs the variable agent run.

### Step 1 — Connect to the browser that can reach CS
`list_connected_browsers` → **AskUserQuestion** to let the user pick (the protocol
requires a human choice here) → `select_browser`. Pick the **personal** profile; a
proxied/managed profile fails CS with `ERR_CONNECTION_REFUSED`.

### Step 2 — Authenticate (daemon-scoped magic link)
```bash
scripts/cs_auth.sh          # prints a single-use, ~3-min login URL, host = 127.0.0.1
```
`navigate` the tab to that URL, then click the **"Sign in"** button (a still-valid
cached session may skip straight to the projects list — that's fine). Generate the
link immediately before navigating — it expires fast, and a daemon restart expires
any login.

### Step 3 — Create / open a project
Paste **Block A** of [`scripts/cs_drive.js`](scripts/cs_drive.js) into
`javascript_tool` (substitute the project name). It clicks *New project*, sets the
name via React's native setter, clicks *Create*, and waits for the `/projects/` nav —
all in one call. Returns `{ok, url}`.

### Step 4 — Submit a prompt (verified fully-programmatic)
Paste **Block B**. On the current build the composer is a **light-DOM** contenteditable
and the verified submit is: locate it → `execCommand insertText` (React registers it) →
click the **"Send"** button that materializes once there's text — no coordinate click,
no Enter key. (Block B also pierces shadow DOM for older builds and always returns the
composer's `center` for a precise coordinate fallback; it clicks the *exact* "Send",
never "More send options".)
- `{submitted:true}` → run started, done.
- `{found:true, inserted:true, submitted:false, center}` → MCP `key Return` (the composer
  is focused), or coordinate-click `center` → `type` → `key Return`.
- `{found:false}` → run **Block D** to re-find the composer (a build changed it).

### Step 5 — Absorb the agent run with the settle poll
Paste **Block C**. It loops *inside the page*, **auto-clicks the "Allow" approval gate**
the instant it appears, and treats the *Stop* button as the busy signal. It is **bounded
to ~35s** because the `javascript_tool` eval hard-times-out near 45s (CS runs routinely
exceed that):
- `{settled:true}` → run done; go read the DB.
- `{stillRunning:true}` → it hit the cap mid-run — **paste Block C again** until settled.

A run under ~35s is ONE call; a longer run is a few — still far fewer turns than checking
every turn. Tip: set **"Allow globally" for python** once (Customize → Permissions) to
drop the per-cell approval turn entirely.

### Step 6 — Read the results from the DB, not the UI
Go back to **Mode 1**: `python3 scripts/cs_provenance.py show <project>`. That is the
authoritative, exact read of what the run produced — filenames, checksums, the cone.

---

## The core technique (why this is fast)

The cost floor is **~1 LLM-turn per browser interaction**, and the two things that
actually slow a driving session are (a) screenshot payloads and (b) waiting on CS's
own agent. So: **batch the deterministic prefix**, use text/DOM reads over screenshots,
and **collapse the variable-length agent run into a settle poll** (Block C) — one
in-page call for a short run, a handful for a long one (each bounded to ~35 s by the
`javascript_tool` eval limit) — instead of a read-and-check turn every few seconds. A
real run showed ~30 s of CS agent time can otherwise carry ~130 s of driving overhead —
almost all of it avoidable.

When a submit path breaks in a new CS build, paste **Block D** (diagnose): it reports
the editable candidates and action buttons with their rects and labels so you can
re-aim without a screenshot.

See [`RECIPE.md`](RECIPE.md) for access details, the DB schema map, and the full
gotcha list with fixes.
