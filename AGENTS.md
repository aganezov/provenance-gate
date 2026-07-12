# Repository guide for AI agents

Conventions for AI agents and humans working in **provenance-gate** —
a deterministic, claim-level **trust gate** for agentic science that runs **read-only** over
Claude Science (CS). Green means a reported value *faithfully reflects a computation*
(transport), never that the analysis is correct. Trust-critical checks are deterministic
code; models only propose, humans confirm/attest.

> Committed on purpose — the shared rulebook every contributor reads. Keep it
> **public-safe**: process and conventions only; never put secrets or unpatched-vuln
> details in tracked files.

## Contribution workflow

1. **Branch** off `main` as `<type>/<slug>` (e.g. `feat/m1-faithfulness`); never commit to `main`.
2. **Tests travel with the change** — new behavior ships with tests; bug fixes go test-first
   (a failing regression test that fails for the *right* reason, then the fix).
3. **Small [Conventional Commits](https://www.conventionalcommits.org/)** —
   `feat:` · `fix:` · `chore:` · `docs:` · `ci:`.
4. **PR with a focused diff** — file adjacent bugs as *new* issues; don't let an
   autoformatter pull unrelated code into it.

## Review & merge

- **Deterministic gates:** `ruff` + `pytest` (see `.github/workflows/ci.yml`, ships with M0). Read pass/fail
  from the authoritative run conclusion, and diagnose any failure from the real logs.
- **Dual review is advisory** — neither blocks. **Wait for both bots on the _latest_ commit
  before merging.** Auto-review fires on PR **open**; after you push new commits, re-trigger
  both reviewers explicitly — a review left on an earlier commit is stale, not latest-commit
  coverage.
- A 👍 reaction (often no comment) = no findings; a comment = findings. Reviews can lag —
  wait rather than re-trigger.
- **Merge when clean:** 0 blockers, gates green, mergeable. Never merge over an actionable
  finding — fix it or file a follow-up issue.
- **Verify a review's claims before acting** — reviewers can cite the wrong line, fabricate a
  SHA/API, or misstate impact.

> **Before flipping the repo public** (tracked TODO): revisit the review-automation trust
> boundary — (1) auto-review passes the Claude secret in a `pull_request` run that reads
> PR-controlled content (acceptable for a private, solo repo; scope/harden when public), and
> (2) external/fork PRs can't currently receive the Claude review (the trusted-author gate
> blocks them) — add a maintainer-triggered external path or document the limitation.

## Trust & safety (project-specific)

The gate is a **read-only overlay**: it must **never mutate CS** — `operon-cli.db` is opened
`mode=ro`, and our sidecar is the only thing we write. **Determinism is the product**:
trust-critical checks (faithfulness, conflict, currency, predicate execution) must be
reproducible, not model-judged. Later milestones execute **user/agent-authored extractor &
predicate code** against frozen artifacts — treat those inputs as **untrusted** (guard
against injection, unsafe eval, and path traversal).

## Attribution

Only **commit messages** carry the `Co-Authored-By:` trailer. PR bodies, issues, and comments
carry **no** trailer or footer.

## Code review output contract (BOTH reviewers follow this)

**Review priorities (in order):** correctness of the deterministic checks; the read-only
invariant (`operon-cli.db` opened `mode=ro`, never mutated); safe SQLite/file handling;
untrusted-input hygiene (user/agent-authored extractor & predicate code is UNTRUSTED — guard
against injection, unsafe eval, path traversal); maintainability.

When reviewing a pull request, post **one structured summary comment** shaped so a maintainer
can triage it in two seconds and expand only what matters:

1. **Verdict line + counts.**
   `## 🤖 <Reviewer> review — <emoji> <disposition>`
   then `> <one-sentence summary>. **N blockers · M suggestions · K nits**`
2. **Findings table** — `| Sev | Location | Finding |`, one row per issue, with `file:line`.
3. **One collapsed `<details>` per finding** — the `<summary>` is the glanceable title
   (severity + `file:line` + short title); the body holds the explanation, why it matters,
   and a ` ```suggestion ` block when a concrete fix applies.

**Severity taxonomy (shared):** 🔴 Blocker · 🟠 High · 🟡 Medium · 🔵 Nit · 💭 Question.

**Anti-noise rules:**

- Do **not** comment on formatting/style — `ruff` owns that.
- Do **not** restate the diff or narrate what the PR obviously does.
- Prioritize correctness, the **read-only / determinism invariants**, and untrusted-input
  handling over personal preference.
- Be specific and cite `file:line`. Keep it advisory.

End with a one-line footer naming the reviewer/model (so when both bots comment, it is clear
who said what).
