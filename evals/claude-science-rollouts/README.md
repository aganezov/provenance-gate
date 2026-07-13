# claude-science-rollouts

A browser-driven harness that generates controlled, automated Claude Science rollouts and captures
their provenance evidence for the [provenance gate](../../README.md).

Each episode runs a Claude Science scenario under a condition (e.g. baseline vs. a provenance
Agent-Context) and freezes what the rollout produced — the agent's behavior, the built-in reviewer's
verdicts, and the project's provenance lineage — into an immutable, content-hashed snapshot. That
snapshot is the deliverable: a rollout that can be scored after the fact, re-scored, and compared
byte-for-byte without ever going back to a live Claude Science.

## Run one rollout

```
uv run cs-rollout \
  --scenario scenarios/pbmc_figure_package.json \
  --trial bare \
  --fixture scenarios/fixtures/pbmc_tiny_seed.csv \
  --model-label "Opus 4.8" --expected-model-identifier claude-opus-4-8 \
  --origin http://localhost:8875 \
  --browser-owner <authenticated-playwright-profile-owner> \
  --source-db <path/to/operon-cli.db> \
  --run-root <external/run/dir> \
  --expected-skill-count <N> \
  --expected-skill-hash <sha256> --expected-context-hash <sha256> \
  --deadline-ms 600000
```

One episode attaches to a live Claude Science session at `--origin`, pins the model, verifies the
enabled-skill and context baseline, drives the scenario turn by turn through the browser, and freezes
the evidence under `--run-root`.

## Preconditions

- An **authenticated** Claude Science session reachable at `--origin`, driven through a Playwright
  profile keyed by `--browser-owner`. The harness attaches to an existing profile — it does not sign
  in.
- `--source-db`: the instance's `operon-cli.db`, opened strictly read-only, for the settled
  snapshots.
- The baseline pins (`--expected-skill-count` / `--expected-skill-hash` / `--expected-context-hash`):
  the enabled-skill set and context the rollout must reproduce before any construction. A rollout
  that cannot match the baseline never starts.

## Evidence produced

```
<run-root>/<episode-id>/
  preflight.json          baseline: skills, model, project id
  episode_manifest.json   turn-by-turn evidence + checkpoint annotations
  run_summary.json        the generation record (below)
  snapshots/
    checkpoints/<checkpoint-id>/project.db   a dependency-settled snapshot per checkpoint
    final/project.db                          the terminal snapshot
```

`run_summary.json` records episode and model identity, the baseline hashes, the manifest and snapshot
sha256s, each checkpoint's result, the database-derived final prose or input request, and the detach
outcome. Every `project.db` is a project-scoped operon — audit one directly with
`uv run pg-audit <snapshot>`.

## Failure semantics

- **Checkpoints are non-halting** by default: each evaluates and records its verdict, and the rollout
  runs to completion so a divergence is captured rather than truncated (`halt_on_checkpoint_gate`
  opts into early-stop).
- A turn that ends in a terminal browser shape — a question, navigation drift, a failure — is
  captured as a `terminal_observation`, distinct from a settled answer; the harness witnesses that
  shape and stops the turn rather than fighting it.
- Drive-level failures (deadline, transient contention) fail closed.

## What this harness does not grade

The harness constructs the scenario and captures immutable evidence — it does not score it. Whether a
rollout's behavior is a catch or a miss is a post-hoc judgment over the captured snapshots and prose;
no grading implementation ships here.

## Included scenarios

- `scenarios/pbmc_figure_package.json` — a PBMC figure-package with a built-in version-mixing trap: a
  stricter-QC revision re-versions the filtered dataset and regenerates only one downstream branch.
  See [the scenario write-up](../../docs/PBMC-ROLLOUT-SCENARIO.md).

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

## Develop

- Python: `uv run pytest` (from this directory).
- Browser boundary: `cd browser && node --test test/*.test.mjs`.
- Lint: `uv run ruff check src tests`.
