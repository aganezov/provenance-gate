# Gate verification, live in Claude Science

The provenance gate was verified end to end **inside Claude Science**, on a real project where the
original agent shipped a version-mixed figure with no provenance warning in its prose. This records
what that verification showed. It is a manual, single-project observation — not an automated suite.

## The project

`manual_pbmc_v3` — a PBMC cross-session reconciliation, hand-built to mirror the
[PBMC scenario](PBMC-ROLLOUT-SCENARIO.md): baseline QC (`min_genes=200`) → a reviewer-driven strict-QC
revision (`min_genes=500`) that re-ran only the IFN branch → a final figure assembled from a mix of
old- and new-QC panels. Across 103 assistant messages over five conversations, the prose carried **no
provenance or version warning**. The conflict lived entirely in the recorded lineage.

## The gate reproduced the server audit exactly

Run in-CS (the `host.query` reader → the inlined kernel → the verdict), the skill returned the same
answer the external server reader computes: **13 cells, 9 flagged** — `version_mix` on the cells that
reconverge `cells.qc.csv` + `qc_params.csv` at v1 and v2, `stale_input` on the cells built from the
superseded versions. Cell-by-cell, artifact-by-artifact, version-by-version identical: the in-CS
reader and the server reader agree, live, on real data. All six helpers ran — `audit_project`,
`audit_input_lineage`, `review_chat`, `review_subgraph`, `review_selection`, `render_cockpit` — and
the cockpit rendered the DAG coloured clean/stale/mix with per-cell inspectors.

## The gate holds the fact the agent talks you out of

The most telling moment was not a conflict a reviewer missed — it was one the agent **actively
rationalized away**. Handed the `version_mix` flag in one conversation, the agent concluded it was
*"benign — the stale v1 is superseded, not consumed."* Its own recorded lineage contradicted that
(the cell that produced `cells.qc.csv v2` had consumed the v1, so the v1 params were genuinely in the
closure), and its own proposed fix conceded it. A human reading only the confident prose would have
shipped it. The gate held the recorded structural fact and did not move.

Handed the same figure's full lineage cone in a different view, the same agent read the conflict
straight — "not coherent, confirmed." The gate's verdict was identical both times; only the prose
moved. That is the argument for a deterministic gate: it resists not just an inattentive reviewer but
a persuasive one.

## Does the agent reach for the gate on its own?

The gate is a capability; the harder question is whether an agent invokes it when it is about to do
something the gate would catch. Tested on a disposable repro — a config revised v1→v2 with one
downstream output left on the old version — asked in a fresh chat with no provenance words to combine
the two:

- **Skill description as the trigger** — passive or imperative wording in the skill catalog: the gate
  **did not fire** in the probes we ran. The agent grabbed each file's latest version and shipped an objectively wrong
  merge. The catalog surfaces skills as opt-in reference, and the agent's "this is trivial" prior
  wins — precisely on the routine-looking tasks where silent misses hide.
- **A standing policy in the project's Agent Context** — the same imperative, pre-work wording,
  injected into every agent's system prompt: **a full autonomous catch.** The agent audited the
  lineage before combining, flagged the stale foundation, wrote nothing, and escalated.

The sharp conclusion: it took a standing policy in the operating context, not a better skill
description, to trigger autonomous use. The pattern is **skill = deterministic capability,
Agent-Context policy = trigger**; when the platform ships save-artifact hooks, that policy becomes a
hard gate.

## Honest limitations

- **Capture gap.** The gate flags staleness only on dependencies Claude Science actually recorded.
  When an agent extracts a scalar (hard-codes a threshold) instead of loading an artifact, no edge is
  recorded and the gate cannot see it. Not a gate error — it faithfully reports the recorded
  provenance — but it bounds real-world coverage.
- **Re-gate gap.** An agent tends to cache a "clean" verdict and skip re-checking on a later
  derivation ("nothing changed since"), an assumption it cannot verify across conversations — which
  is exactly what cross-session divergence breaks.
- **Trigger dependency.** Autonomous use depends on the Agent-Context policy today; a hard,
  unavoidable gate waits on platform save-artifact hooks.
- **Publish-based verification.** The verification ran on a *published* skill — the path Claude
  Science actually deploys. Running the same skill as an unpublished draft is not seamless on this
  build (the loader refuses drafts; the helpers need the repl kernel), so publish → test → unpublish
  was the loop used.
