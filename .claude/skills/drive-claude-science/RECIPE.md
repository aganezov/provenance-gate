# CS driving — access recipe, DB map, and gotchas

Operational reference for the `drive-claude-science` skill. Read [`SKILL.md`](SKILL.md)
first for the workflow; this file is the detail you reach for when something breaks or
when you need the exact schema.

Verified against operon build `0.1.16-dev.20260707`. CS internals drift between builds
— when a selector or table shape looks wrong, re-check with **Block D** (diagnose) for
the DOM and `sqlite3 "file:$DB?mode=ro" ".schema <table>"` for the store.

---

## 1. Access recipe (browser)

**a. Right browser.** CS is only reachable from the Chrome instance whose profile can
actually route to the loopback server. A proxied/managed profile fails with
`ERR_CONNECTION_REFUSED`; the **personal** profile works. The claude-in-chrome flow:
`list_connected_browsers` → **AskUserQuestion** (the protocol requires a human to pick
the browser) → `select_browser` → `navigate`.

**b. `127.0.0.1`, not `localhost`.** CS binds IPv4-only. `localhost` resolves to `::1`
first on macOS and the connection refuses. Everything — the login URL, every navigate —
uses `127.0.0.1:8765`. `cs_auth.sh` already rewrites the host for you.

**c. Auth = a daemon-scoped magic link.**
```bash
scripts/cs_auth.sh          # -> http://127.0.0.1:8765/?nonce=…   (single-use, ~3 min)
```
`navigate` the automation tab to that URL, then click the **"Sign in"** button. Notes:
- The nonce is **single-use and expires in ~3 minutes** — generate it immediately
  before you navigate, not earlier.
- A **daemon restart expires any existing login** (and any unused link). If pages start
  bouncing to a login screen mid-session, the daemon was restarted — get a fresh link.
- `cs_auth.sh` refuses if `claude-science status` isn't `"running": true`.

**d. Is it even up?**
```bash
claude-science status       # JSON: {"running":true,"port":8765,"version":…}
```

---

## 2. DB read recipe (read-only)

The store is the fast, exact, side-effect-free way to read anything CS has produced —
and the blueprint for any headless CS reader (e.g. a provenance gate).

```bash
DB=~/.claude-science/orgs/<org-uuid>/operon-cli.db
sqlite3 "file:$DB?mode=ro" "…"        # ALWAYS mode=ro; never write this file
```
`cs_provenance.py` does the discovery (`--db` → `$CS_DB` → `active-org.json` → glob) and
opens with `mode=ro` (not `immutable=1`, because the daemon may be writing via WAL; `ro`
still gives a consistent snapshot without taking a lock).

### Table map (the primitives)

| Table                    | What it is                                    | Key columns |
|--------------------------|-----------------------------------------------|-------------|
| `projects`               | a project                                     | `id`, `name`, `uploads_frame_id` |
| `frames`                 | interaction tree; a session = **UPLOADS + OPERON + REVIEWER×N** | `id`, `parent_frame_id`, `agent_name`, `status`, `project_id`, `conversation_type` |
| `artifacts`              | a named output slot                           | `id`, `filename`, `latest_version_id`, `is_user_upload`, `superseded_by_artifact_id` |
| `artifact_versions`      | a **frozen** version — the faithfulness pin   | `id`, `artifact_id`, `version_number`, `checksum` (sha256), `storage_path`, `producing_cell_id` |
| `artifact_dependencies`  | the **consumes DAG** edges                    | `artifact_version_id` → `depends_on_version_id`, labeled `reference_name` |
| `execution_log`          | code cells                                    | `frame_id`, `cell_index`, `source`, `stdout`, `exit_status`, `files_written` (JSON), `files_read` |
| `verification_checks`    | CS's **advisory** sonnet reviewer (non-blocking) | `root_frame_id`, `artifact_version_id`, `verdict`, `severity`, `evidence` |

### The two derived signals (deterministic, no LLM)
- **Conflict** — ≥2 versions of one artifact appear in a node's cone.
- **Currency** — a cone version is not its artifact's `latest_version_id`.

`cs_provenance.py cone <version-id>` computes both by walking `artifact_dependencies`
transitively.

### Faithfulness pin (verified working)
`storage_path` is **relative to `<org-dir>/artifacts/`** (the sibling of the `.db`).
Recompute and compare:
```bash
# on-disk sha256 == recorded checksum  ==>  the frozen artifact is intact
shasum -a 256 "<org>/artifacts/<storage_path>"   # matches artifact_versions.checksum
```
`execution_log.files_written` stores **absolute workspace paths** (a different root) —
that's the cell's working output, not the frozen artifact. Use `artifact_versions` for
the pin.

---

## 3. Gotchas (each with its fix)

**1. `javascript_tool` needs `await` on async IIFEs.** Without the leading `await`, the
tool returns `{}` (an unawaited Promise) even though the side effects still run — you'll
think it failed when it didn't. Every block in `cs_drive.js` starts with `await`; keep it.

**2. The message composer + submit.** VERIFIED on build `0.1.16-dev.20260707`: the
composer is a **light-DOM** `<div role="textbox" contenteditable>`, and the fully
programmatic submit works — **focus it → `document.execCommand("insertText", …)` (React
registers it) → click the "Send" button that MATERIALIZES once there is text.** No
coordinate click, no Enter key needed. Two sharp edges: (a) once you type, both a
"Send" and a "More send options" button appear — click the **exact** "Send", never the
"more" one (Block B does); (b) an **earlier build encapsulated the composer in shadow
DOM**, where `querySelector` returned 0 — so Block B locates it with a shadow-piercing
BFS (a harmless superset in the light-DOM case) and still returns the editor's `center`
for a *precise* coordinate fallback (MCP click `center` → `type` → `key Return`) instead
of the old hardcoded `[620,677]` that only worked at 1080×768. Buttons
(Send/Stop/Allow/artifact chips) are always in the **light DOM** — plain
`querySelectorAll('button')` reaches them. When a build changes the composer, run
**Block D** to re-aim.

**3. The approval gate is variable.** CS may interrupt a run with an "Allow" / "Allow
globally" permission card. **Fix (two layers):** (a) set **"Allow globally" for python**
once in Customize → Permissions — this removes the per-cell approval turn entirely and is
the single biggest saver; (b) as a backstop, the settle poll (Block C) auto-clicks any
Allow button the instant it appears, so a run never silently blocks on approval.

**4. Large `innerText` reads return "[BLOCKED: Cookie/query string data]".** Returning
page text in bulk from `javascript_tool` is blocked. **Fix:** return small structured
objects only (filenames, counts, a 60-char preview) — never dump innerText. Block C's
`artifactsSeen` matches only elements whose *entire* text is a filename, keeping the
payload tiny; the authoritative read is the DB anyway.

**5. IPv4 / localhost.** (See §1b.) `127.0.0.1` everywhere.

**6. Daemon restart expires links and logins.** (See §1c.) Symptom: mid-session pages
bounce to a login screen. Fix: fresh `cs_auth.sh` link, click Sign in again.

**7. `javascript_tool` (CDP eval) hard-times-out around 45s.** A single in-page call
that runs longer fails with *"Runtime.evaluate timed out … the renderer may be frozen"* —
even though the page keeps working. This bites the settle poll hardest, because CS
agent runs routinely exceed 45s (agent spin-up + compute + reviewer). **Fix:** cap any
in-page loop at **~35s** and make it re-invokable. Block C does exactly this: it returns
`{settled:true}` when the run finishes, or `{stillRunning:true}` when it hits the ~35s
cap while still busy — in which case you just call Block C again. A run under ~35s
settles in one call; a 90s run takes ~3 calls (still far fewer turns than polling every
turn). Never raise the cap toward 45s "to be safe" — that is the failure, not the fix.

---

## 4. Speed model (why the workflow is shaped this way)

- Floor ≈ **1 LLM-turn per browser interaction**. You cannot go below it; you can only
  reduce the *number* of interactions.
- The dominant costs are **screenshot payloads** (100 KB+, slowing every turn) and
  **waiting on CS's own agent** — not the choice of browser MCP.
- Therefore: text/DOM reads over screenshots; **batch the deterministic prefix**
  (create → submit are fixed steps); and **collapse the variable agent run into a
  bounded settle poll** (Block C — one call for a short run, a few for a long one,
  each capped at ~35s per gotcha #7) rather than many read-then-check turns.
- Measured on a real run: ~30 s of CS agent work carried ~130 s of driving overhead
  before this shaping — almost all of it removable.
- For pure throughput (fixtures, machine reads) skip the browser entirely and read the
  DB. **Browser = for the live demo; DB = for machine reads.**

---

## 5. Known fixtures (real paused CS runs, for testing the reader)

| Project           | id                | Shape |
|-------------------|-------------------|-------|
| `drive-smoke-test`| `proj_539b55ffdfef` | `stats.csv → note.txt` cone; the minimal end-to-end |
| `drive-speed-1`   | `proj_74d3568e014b` | single `result.csv` |
| `gate-3step-demo` | `proj_1b289f0aa0f3` | multi-version cone with a real **conflict + currency** case (`decision.json` rests on `cohort.json` v1 *and* v2) |

```bash
python3 scripts/cs_provenance.py show drive-smoke-test
python3 scripts/cs_provenance.py cone 16e811c4      # decision.json@v1 -> flags conflict+stale
```
