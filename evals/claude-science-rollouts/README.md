# claude-science-rollouts

A browser-driven harness that generates controlled, automated Claude Science rollouts and captures
their provenance evidence for the [provenance gate](../../README.md).

Each episode runs a Claude Science scenario under a condition (e.g. baseline vs. a provenance
Agent-Context) and freezes what the rollout produced — the agent's behavior, the built-in reviewer's
verdicts, and the project's provenance lineage — into an immutable, content-hashed snapshot. That
snapshot is the deliverable: a rollout that can be scored after the fact, re-scored, and compared
byte-for-byte without ever going back to a live Claude Science.

## Architecture

Python is the control plane; a Node `@playwright/cli` subpackage is the browser boundary.

```
Python harness
  → subprocess → one bounded JavaScript browser primitive
              → operates Claude Science via Playwright CLI
              → returns one versioned, bounded JSON result
  → Python records evidence and decides the next step
```

- **Python owns** scenario compilation, episode/replicate orchestration, browser-lease policy,
  conditions, approval/simulated-human policy, read-only Operon reads (project snapshots and the
  dependency-closure walk), timeout recovery, checkpoints, and evidence manifests.
- **JavaScript owns** only the browser boundary — a small set of Playwright-CLI primitives, in the
  `browser/` subpackage.

## Independence

The harness reads Claude Science strictly read-only, through its **own** raw-SQL closure walk over the
operon DB — it never imports the gate, so nothing here leans on the gate's own code. Python and
JavaScript test suites run separately.
