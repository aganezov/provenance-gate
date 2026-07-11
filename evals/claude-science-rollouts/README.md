# claude-science-rollouts

A browser-driven evaluation harness that measures the [provenance gate](../../README.md)'s thesis with
controlled, automated Claude Science rollouts.

Each episode runs a Claude Science scenario under a condition (e.g. baseline vs. a provenance
Agent-Context), captures the agent's behavior, the built-in reviewer's verdicts, and the project's
provenance lineage, and scores — deterministically — whether the agent and reviewer catch a
version-inconsistency that the deterministic gate catches.

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
  conditions, approval/simulated-human policy, the read-only Operon SQL oracle, timeout recovery,
  checkpoints, evidence manifests, and scoring/aggregation.
- **JavaScript owns** only the browser boundary — a small set of Playwright-CLI primitives, in the
  `browser/` subpackage.

## Independence

The harness's structural ground truth is its **own** raw-SQL closure walk over the operon DB — it does
not import the gate, so the evaluation never judges the gate with the gate's own code. The reads are
strictly read-only. Python and JavaScript test suites run separately.
