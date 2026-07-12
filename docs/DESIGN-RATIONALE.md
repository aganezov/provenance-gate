# Provenance Gate — Design Rationale

What the gate assumes, what it checks and refuses to check, why the non-obvious decisions went the way
they did, and where it falls short. If a choice looks odd, the reason is here.

## 1. What the gate is (and is not)

It is a read-only observer over Claude Science. It watches the provenance DAG an agent builds and
computes two verdicts per computation: whether the computation rests on stale data, or on two
conflicting versions of the same artifact. Both come out of the graph, with no model in the path.
That is deliberate. A computed verdict doesn't depend on a model reading its own work correctly.

It is not a correctness checker, not a faithfulness checker (content vs. hash), not a linter, and not
an agent. It says nothing about whether an analysis is right, only whether it was built on current,
consistent inputs. It can miss a conflict that lives in the provenance rather than the text, but how
often that matters is an empirical question we haven't measured (§5).

### Scope: the deterministic slice of a larger design

The full design is a four-signal gate:

- faithfulness — a located value equals the frozen artifact;
- assumptions — a human attests them, or discharges them with a predicate; un-examined ones read grey;
- provenance — the deterministic conflict and currency signals;
- comparability — joins where two arms share a root but were processed differently, triaged by reading
  the code and confirmed by a human.

These roll into a four-colour verdict, with human trust acts (confirm, attest, link) on top.

We built the provenance slice: `version_mix` (conflict) and `stale_input` (currency), computed on read
and storing nothing, plus the cockpit and the review briefs. We also built half of comparability, the
code-reading scan, run over a region a human picks (D10).

What we didn't build, and the README doesn't claim:

- Faithfulness, assumptions, attestations, and links. These are the human-owned layers, and they all
  need a writable overlay store to persist confirmations and attestations per project. The substrate
  didn't give us a store we could rely on (no persistent read-write overlay, no stateful annotations),
  and we ran out of time, so we shipped the layer that needs no store at all.
- The other half of comparability: detecting the recombination site automatically, and persisting the
  attestation. A person supplies the detection by selecting the fork in the cockpit.

That is a scope call forced by the substrate, not a blind spot in the design. [HEADROOM.md](HEADROOM.md)
covers what more access would open up.

## 2. Assumptions

| # | We assume | Because | If it's wrong |
|---|---|---|---|
| A1 | The provenance is a DAG. | Artifacts are immutable versions; a version only depends on ones that already existed. | A cycle can't happen in real data. If one did, the audit degrades to a per-node best effort instead of crashing. |
| A2 | CS is read-only to us. | We only issue reads. | We never call a write path. |
| A3 | A cell is an agent turn: it reads its inputs, reasons over them, and writes outputs. | That is what a CS cell is. | Underlies D3. |
| A4 | Staleness is decidable from one row. | Each version row carries its artifact's current head, so currency is known on read. | Structural. |
| A5 | Version ids are stable and unique, and the head is authoritative. | CS ids are stable. | Naming degrades gracefully (D8); a real mix is still flagged. |

## 3. What it checks, and what it refuses to

The two verdicts:

- `stale_input` — a cell reads a non-current version of some artifact. A cell that reads v1 to write v2
  is revising, not reading stale, so it is excluded.
- `version_mix` — a cell's consumed lineage reaches two live versions of one artifact.

Out of scope, on purpose:

- Faithfulness (content vs. hash). The checksum is carried but unused; it's a seam, not a claim.
- Whether an analysis is correct.
- Anything downstream. Verdicts only walk ancestry.
- Stale outputs. Producing a version that later gets superseded isn't flagged; an old branch is
  harmless. Only reading stale data is (D4).

## 4. Decisions

### D1 — One core, two readers
The pure `core/` feeds two adapters: a server reader over raw SQL, and an in-CS kernel over
`host.query`. The same core drives both a live cockpit (in the server setup, where `server.py` serves
`/api/graph` and the cockpit polls it) and the static in-CS snapshot. The two readers can't share
code: the kernel is inlined into one self-contained file with no imports, so it can't pull in the
server package. Parity tests keep them deriving the same graph. It looks like duplication, but the
alternative is a shared import, which breaks the inline kernel.

### D2 — version_mix over consumed lineage, per version
Detection walks the specific versions a node consumed, one cone per output version, not the whole
output surface of the producing cell. An earlier per-cell cone let a co-output sibling leak in: a `qc`
file a cell wrote alongside the file you actually read would register in your lineage as a mix, even
for versions on the same revision line. Per-version cones fix that, and they make a focused cockpit
render agree with the full audit. We considered reusing the cockpit's baked verdicts to save a
recompute and dropped it, because a focused render can bake an incomplete verdict and hand a review a
false clean.

### D3 — Conservative on inputs, precise on outputs
Every output of a cell inherits all of the cell's inputs; sibling outputs don't. The explicit
file-read edges are a lower bound on what an output really depends on, for two reasons. First, a cell
reasons over everything it read, and reasoning leaves no file-read edge. Second, CS's capture is
shallow and cell-granular: it doesn't reliably record which output used which input, so per-output
precision isn't available anyway. A trust check should err toward false positives over false
negatives, so we keep the over-approximation. Sibling outputs are different; they're peers, not
ancestors, so they're excluded.

This one nearly got "fixed" as a bug before we saw it was right. The flag applies to a cell that
consumed two divergent versions. A cell that only revised its own output — wrote v1, then v2 — isn't
mixing; subsumption collapses it to the latest, and a downstream reading only v2 stays clean (D4).

### D4 — Upstream only; stale input, not stale output
A mix needs actual consumption of two versions; co-production alone doesn't count. Staleness is judged
on a cell's inputs. Producing a version that's since been superseded isn't flagged. The gate is about
forward risk, building on stale or mixed data, not about the existence of old versions. An abandoned
branch is fine; reading from it isn't.

### D5 — Two surfaces, joined by copy-paste
The agent side is stateless functions that return JSON. The user side is a static cockpit. "Review →"
copies a prompt the person pastes to the agent. It's indirect because the substrate gives no
persistent UI tile, no way for the page to call back, and no stateful annotations, so the two sides
can't share live state. One upside is that the trust signal stays deterministic and inspectable
instead of turning into another opaque agent channel.

### D6 — Selective review audits the whole graph
`review_selection(nodes)` shows only the nodes you picked (a fork, minus a trunk you trust), but
computes the verdicts over the full project. A focused brief shouldn't go blind to a conflict sitting
in the trunk you excluded. `trusted_inputs` lists what the selection consumes from outside itself, so
the boundary is explicit.

### D7 — audit_input_lineage checks the foundation
The pre-write check looks for staleness or a mix in the inputs' ancestry, not just in a hypothetical
merge of the named files. The named inputs are current by construction, so a plain merge check can't
see the case that actually bites: an input that is the latest of its artifact but was itself built
from a since-superseded source. The foundation audit catches that.

### D8 — Deterministic, and a detected mix is never dropped quietly
Every verdict is a pure function of the graph: topological order with id tie-breaks, head-join
currency, set-based cones, issue naming by the lowest version id. When a mix is detected it is always
reported. On corrupt or partial data it is reported with degraded naming rather than crashing or going
clean. The worst thing a trust gate can do is report clean when it isn't, so the code fails loud
rather than quiet.

### D9 — The kernel gate
`core/` and the in-CS reader are inlined into one `kernel.py` that has to load under CS's loader: no
top-level `_`-names, classes, or `try`, parseable on Python 3.9, no external imports. The build nests
the private helpers inside a `pg_impl(host)` wrapper so only the public entry points sit at the top
level. CS runs the skill under a load gate, and a self-contained single file is the shape that gets
through.

### D10 — The review hand-off is a manual comparability scan
`review_selection` and `review_subgraph`, reached from "Review →", set up a code-reading scan over a
region a human picked. The brief gives the agent the lineage, the raw-input boundary, the deterministic
flags, and the cell ids whose code to fetch, which is the scan the full design describes: read the code
of each arm. The scan is real, and better than a bare LLM pass because it carries the structural flags
alongside the code. The detection is manual, since a person spots the fork; auto-detecting a
shared-root-processed-differently join is new LCA/path-divergence code we didn't get to, and
`version_mix` only auto-detects the same-artifact case. The attestation is ephemeral, since there is
no store.

It's a shape, not a wall. The LLM is already the agent, which reasons over the brief, so the scan
already runs, and `host.llm` is there if we ever want a separate in-skill call. The only thing keeping
a person in the loop is that the cockpit page can't call the skill (see [HEADROOM.md](HEADROOM.md)).

## 5. Limitations

- Not published, so no autonomous trigger yet. This is the gap we most want to close: publishing is
  what lets the agent run `audit_input_lineage` before it computes, on its own. Everything reactive
  works now.
- The in-CS cockpit is a snapshot, not live. It runs live in the server setup (polling `/api/graph`);
  in-CS it re-renders on demand because there is no persistent tile. We looked for one; it seems
  possible but is gated to trusted vendors right now, and we haven't verified what it can do. To keep
  snapshots small, the big assets (the ~670 KB cytoscape/dagre bundle, the app CSS and JS) are saved
  once as artifacts and referenced by marker, so a render is a few KB rather than ~770 KB.
- The hand-off is copy-paste; the page can't call back (D5).
- No faithfulness check yet.
- The conservative input rule over-flags downstream of a cell that read two divergent versions of one
  artifact (D3). That's the safe direction, and the cell itself is flagged too.
- Cycle handling is best-effort and order-dependent, on an input that can't occur in valid data (A1).
- No effectiveness numbers. How often it catches something a reviewer would have missed, and how often
  that matters, is for the eval harness.

## 6. Validation

- Unit tests over hand-built graphs: the two checks, the revision case, co-output isolation, external
  inputs, deterministic issue fields, null-version mixes, and the transitive-vs-direct distinction.
- Reader parity: the server and in-CS readers derive the same graph, so the surfaces don't drift
  without a test noticing.
- Replays on live CS projects: the co-output false positive is gone, real mixes stay, and a focused
  render matches the full audit.
- Every substantive change went through an adversarial review and a multi-angle pass; findings were
  fixed or written down here.
