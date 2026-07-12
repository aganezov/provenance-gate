# Constraints & Headroom

What more access to the substrate would open up.

The gate was built to work within the current environment's limits, and the deterministic core is
stateless and doesn't care which surface calls it. So most of these limits are seams rather than
walls: lifting one extends the same code instead of forcing a rewrite. Below, for each limit, what it
forces now and what it would unlock.

## The limits

| Limit | What it forces now | What lifting it opens |
|---|---|---|
| The cockpit page can't call the skill or the agent. | The hand-off from cockpit to agent is a copy-paste. | Let the page call the skill: click a fork, the agent gets the brief and reasons, no person in the middle. This is wiring, not model access. |
| No writable overlay store (storage and write limits) | Stateless deterministic checks only; nothing persists between runs | The human-owned layer: assumptions, surfaced-value links, version-stamped confirmations, and the full four-colour verdict. Recurring attestations could graduate into new deterministic checks over time. |
| No persistent UI tile in-CS. A tile seems possible but is gated to trusted vendors right now, and its capabilities are unverified. | The in-CS cockpit is a re-rendered snapshot (it's already live in the server setup). | A live cockpit inside CS, updating as the agent works. Snapshots become optional, kept only when you want a frozen record. |
| No stateful agent annotations | Every helper is a pure function | The agent can leave marks (reviewed, waived, owned) and carry trust state across a session. |
| Shallow, cell-granular dependency capture | A conservative over-approximation on inputs; comparability sites can't be detected automatically | Precise per-output lineage, and automatic detection of shared-root-processed-differently joins, so nobody has to spot the fork by eye. |
| The skill runs as a draft, not published | Human-invoked, or agent-invoked with a draft-activation phrase | The autonomous trigger: the agent runs the pre-write check before an expensive step on its own. |

On the first row: the LLM is already in the loop, because it's the agent, which runs the skill and
reasons over the briefs. `host.llm` is there for a separate in-skill call, but we route through the
agent. So closing that gap is wiring, not model access.

## Cheaper at scale, with a store

The two checks age differently, which a persistent store could exploit:

- `version_mix` is stable. A node's cone never changes, since artifacts are immutable and the graph
  only grows, so a mix verdict can be computed once and kept. Nothing added elsewhere invalidates it.
- `stale_input` isn't. A new version of a consumed artifact, made anywhere, can turn an old node
  stale, so it has to be rechecked. It is cheap per row, though.

On a big project that is the difference between a subgraph view that scales and one that doesn't: read
the cached mix verdicts, recompute only the cheap staleness. Today, with no store, every view
recomputes from scratch.

## Why these are seams, not a wishlist

Each one lands on something already in the architecture:

- The verdict is computed on read and stored nowhere, so a persisted attestation layer is additive.
  The design already separates what's computed (never stored) from what a human owns (the store we
  didn't build).
- The core doesn't know about its callers; it's reached through two adapters. A third surface, a live
  tile or a scan client, is another thin adapter.
- The review brief already names the cells whose code to fetch, so an in-loop comparability scan is
  the same brief consumed by the agent instead of pasted by a person.
- The two checks are the floor of a four-signal design. Faithfulness, attestation, and auto-detected
  comparability sit above them without changing what's here.

The gate today is that floor. Close the cockpit-to-agent gap, add a store and a live surface, and the
same core reaches the rest of the design. Most of that distance is building and wiring, not access.
