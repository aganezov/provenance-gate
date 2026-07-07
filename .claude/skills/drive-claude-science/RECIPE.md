# CS driving — access recipe and gotchas

Operational reference for the `drive-claude-science` skill. Read [`SKILL.md`](SKILL.md)
first for the workflow; this file is the detail you reach for when something breaks.

**Scope reminder:** this skill is a **browser driver only**. It does not read
`operon-cli.db` and does not compute provenance / DAG / cones / checksums — the
surrounding app owns that. Everything here is about driving the CS UI.

Verified against operon build `0.1.16-dev.20260707`. CS internals drift between builds —
when a selector looks wrong, re-check with **Block D** (diagnose).

---

## 1. Access recipe (browser)

**a. Right browser.** CS is only reachable from the Chrome instance whose profile can
route to the loopback server. A proxied/managed profile fails with
`ERR_CONNECTION_REFUSED`; the **personal** profile works. Flow: `list_connected_browsers`
→ **AskUserQuestion** (the protocol requires a human to pick the browser) →
`select_browser` → `navigate`.

**b. `127.0.0.1`, not `localhost`.** CS binds IPv4-only. `localhost` resolves to `::1`
first on macOS and the connection refuses. Every navigate uses `127.0.0.1:8765`;
`cs_auth.sh` already rewrites the host for you.

**c. Auth = a daemon-scoped magic link.**
```bash
scripts/cs_auth.sh          # -> http://127.0.0.1:8765/?nonce=…   (single-use, ~3 min)
```
`navigate` the automation tab to that URL, then click **"Sign in"** (a still-valid
cached session may skip straight to the projects list). The nonce is single-use and
expires in ~3 minutes — generate it immediately before navigating. A **daemon restart
expires any existing login**; if pages start bouncing to a login screen mid-session, get
a fresh link.

**d. Is it up?**
```bash
claude-science status       # JSON: {"running":true,"port":8765,"version":…,"started_at":…}
```
`started_at` also tells you whether the daemon restarted since you logged in.

---

## 2. Gotchas (each with its fix)

**1. `javascript_tool` needs `await` on async IIFEs.** Without the leading `await`, the
tool returns `{}` (an unawaited Promise) even though the side effects still run — you'll
think it failed when it didn't. Every block in `cs_drive.js` starts with `await`; keep it.

**2. The composer + submit.** VERIFIED on build `0.1.16-dev.20260707`: the composer is a
**light-DOM** `<div role="textbox" contenteditable>`, and the fully programmatic submit
works — **focus it → `document.execCommand("insertText", …)` (React registers it) → click
the "Send" button that MATERIALIZES once there is text.** No coordinate click, no Enter
key. Two sharp edges: (a) once you type, both a "Send" and a "More send options" button
appear — click the **exact** "Send", never the "more" one (Block B does); (b) an earlier
build encapsulated the composer in **shadow DOM** where `querySelector` returned 0 — so
Block B locates it with a shadow-piercing BFS (a harmless superset in the light-DOM case)
and still returns the editor's `center` for a *precise* coordinate fallback instead of the
old hardcoded `[620,677]` that only worked at 1080×768. **The fallback uses the SAME JS
insert**, not MCP typing: coordinate-click `center` to *focus* the composer, then
`document.execCommand("insertText", false, TEXT)` on `document.activeElement` (no selector
needed — recovers a total selector miss), then click "Send". Verified live that **MCP
`type` does not register in this editor** and **Enter does not submit** — never rely on
either. When a build changes the composer, run **Block D** to re-aim.

**3. The approval gate is variable (auto-click VERIFIED firing).** CS interrupts a run
with an "Allow" / "Allow globally" permission card, typically on the first tool use in a
project. **Fix (two layers):** (a) set **"Allow globally" for python** once in Customize →
Permissions — removes the per-cell approval turn entirely and is the single biggest saver;
(b) as a backstop, the settle poll (Block C) auto-clicks any Allow button the instant it
appears — confirmed firing live on a fresh project (`approvals:1`), so a run never silently
blocks on approval.

**4. Busy detection must use the Stop button's `aria-label`, not its text.** The "Stop"
button that replaces "Send" during a run is an **icon button with empty `textContent`** —
a text-only `/^stop$/` check misses it and the poll would falsely report `settled`. Block
C checks **both** `textContent` and `aria-label` (`/stop/i`); keep both. Presence of Stop
(and absence of Send) is the reliable "still running" signal.

**5. Large `innerText` reads return "[BLOCKED: Cookie/query string data]".** Returning
page text in bulk from `javascript_tool` is blocked. **Fix:** return small structured
objects only (filenames, counts, a short preview) — never dump innerText. Block C's
`artifactsSeen` matches only elements whose *entire* text is a filename, keeping the
payload tiny.

**6. IPv4 / localhost.** (See §1b.) `127.0.0.1` everywhere.

**7. Daemon restart expires links and logins.** (See §1c.)

**8. `javascript_tool` (CDP eval) hard-times-out around 45 s.** A single in-page call that
runs longer fails with *"Runtime.evaluate timed out … the renderer may be frozen"* — even
though the page keeps working. This bites the settle poll hardest, because CS agent runs
routinely exceed 45 s (agent spin-up + compute + reviewer; ~215 s seen for a trivial
task). **Fix:** cap any in-page loop at **~30 s** and make it re-invokable — and count any
**post-loop DOM work against the same budget**. Block C returns `{settled:true}` when the
run finishes, or `{stillRunning:true}` when it hits the cap while still busy — in which case
you just call Block C again. A run under ~30 s settles in one call; a 3-minute run takes ~6
calls (still far fewer turns than polling every turn). Never raise the cap toward 45 s "to
be safe" — that is the failure, not the fix. **Watch the *scan*, not just the loop:**
`artifactsSeen` first walked every element's `textContent` (which concatenates the whole
growing transcript) and added ~8 s *after* a long run — right up against the timeout. Block
C now scans only leaf nodes, stops at 25 hits, and reports `elapsedMs` (loop) separately
from `scanMs` (scan) so the margin stays visible. And note the **other** timeout cause: a
poll can hit the 45 s limit **even under the cap** if CS transiently blocks the shared main
thread during a heavy run (your injected JS runs on that same thread, so it can't return).
Same error text; not a bug in your loop — treat a Block C timeout **exactly like
`stillRunning`** and re-invoke. Seen live.

---

## 3. Speed model (why the workflow is shaped this way)

- Floor ≈ **1 LLM-turn per browser interaction**. You cannot go below it; you can only
  reduce the *number* of interactions.
- The dominant costs are **screenshot payloads** (100 KB+, slowing every turn) and
  **waiting on CS's own agent** — not the choice of browser MCP.
- Therefore: text/DOM reads over screenshots; **batch the deterministic prefix**
  (create → submit are fixed steps); and **collapse each variable agent run into the
  bounded settle poll** (Block C — one call for a short run, a few for a long one, each
  capped at ~30 s per gotcha #8) rather than many read-then-check turns.
- Measured on a real run: ~30 s of CS agent work carried ~130 s of driving overhead
  before this shaping — almost all of it removable.

---

## 4. Disposable test projects

To stress-test the driver, create clearly-named throwaway projects (e.g.
`stress-dag-01`) with Block A and drive multi-turn flows through them. There is **no CLI
verb to delete a project** — deletion is UI-only and irreversible — so name throwaways so
you can find and remove them later in the UI, and don't leave sensitive prompts in them.
