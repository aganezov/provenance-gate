# Cell-level detection is a superset of artifact-level detection

## The statement

> On a transactionally consistent, closure-complete recorded snapshot whose artifact-version graph and
> producer-block contraction are both acyclic, and whose audited producer-block surfaces cover every
> recorded direct input, the block-aggregated audit preserves mixture-detection sensitivity under the
> mapping from consumer versions to producer blocks: every divergent live-version mixture visible in the
> recorded artifact-level edges is detected at the producing block. It may add conservative block-level
> findings and does not preserve output-level attribution. The gate does not yet validate these
> preconditions or fail closed when they break, so the guarantee holds for snapshots that have been
> frozen and structurally checked separately.

Sections 1–4 are graph theory and stand on their own; the recurrences are defined only on the DAGs, so
the theorem says nothing outside them (§5 is what the code does there instead). Sections 5–7 are about
the gate that implements it. The two acyclicity assumptions are named AV-DAG (artifact-version graph)
and CELL-DAG (producer-block contraction), not A1/A2, because those numbers already mean other things in
`DESIGN-RATIONALE.md`.

---

## 1. Model

Let `G = (V, E)` be a finite directed graph, `v ∈ V` an **artifact version**. Edges are
**producer → consumer**: `(u, v) ∈ E` iff `v` directly depends on `u`. `In(v) = { u : (u,v) ∈ E }`. A
topological order of `G` lists every producer before its consumers.

`α : V → A` gives the **artifact identity**.

**Producers and the block map.** Some versions are **exogenous** (uploaded / external, no producing
computation); let `X ⊆ V` be that set. Real computations form **cells** `C`. The **block map**
`β : V → B` is total, where `B = C ∪ { s_x : x ∈ X }`:
- a produced version maps to its **real cell** `β(v) ∈ C`;
- each exogenous `x ∈ X` maps to its own **synthetic singleton block** `s_x`.

Crucially, `In(v) = ∅` does **not** mean "source": a computation that generates data from scratch has a
zero input set but a real cell, and it stays in that cell. Only genuinely producerless versions get
synthetic blocks. `O(p) = { v : β(v) = p }`.

**Contraction.** `G/β = (B, E_B)`, `(p,q) ∈ E_B` iff `p ≠ q` and `∃ (u,v) ∈ E` with `β(u)=p, β(v)=q`;
intra-block (self-loop) edges dropped. Exogenous singleton blocks appear here as ordinary source blocks.

**Cones.** A **cone** `κ : A → 2^V`; order pointwise `⊆`, join `∨` = pointwise union. **Subsumption**
`σ_v(κ) = κ` with `κ(α(v))` replaced by `{v}`; `σ_v` is monotone.

**Mixture.** `κ` *mixes* `a` iff `|κ(a)| ≥ 2`.

**Artifact-level audit** (each vertex a node; no source special case — the empty join handles zero
inputs):
```
Vin(v)  = ⋁_{u∈In(v)} Vout(u)        (empty join when In(v)=∅ ⟹ Vin(v)=∅)
Vout(v) = σ_v(Vin(v))                 (so a zero-input v ⟹ Vout(v) = {α(v):{v}})
```

**Cell-level audit** (per block, seeded relative to the block being processed; blocks processed in a
`G/β` topological order, which exists under CELL-DAG):
```
I(p)      = ⋃_{v∈O(p)} In(v)                      (all recorded direct inputs of the block's outputs)
S(p)      = the input surface actually audited for p       (= node.input_surface in the code)
Seed_p(u) = Cout(u)      if β(u) is a strictly earlier block
          = {α(u):{u}}    if β(u) = p (its cone is not yet materialized)
Base(p)   = ⋁_{u∈S(p)} Seed_p(u)
Cout(v)   = σ_v(Base(β(v)))    for v ∈ O(p)
```
Only `Base(p)` is shared across a block's outputs. Co-production alone does not make sibling outputs
ancestors of one another — one becomes an ancestor only through a recorded intra-block dependency (proof
case 3).

## 2. Formal proof conditions

The theorem holds for a graph satisfying:

1. **Fixed finite endpoint-closed graph, total identities.** Every referenced version is present; `α` and
   `β` are total.
2. **AV-DAG.** `G` is acyclic. (Implies every within-block subgraph is acyclic — a subgraph of a DAG is a
   DAG. Expected of immutable-version provenance, but not schema-enforced.)
3. **CELL-DAG.** `G/β` is acyclic. This is the condition that can actually fail (box); the others hold
   by construction or are cheap to check.
4. **Input-surface coverage.** `I(p) ⊆ S(p)` for every block `p`. Extras only cause conservative flags;
   omitting a recorded direct input breaks the proof. In the implementation `S(p) = I(p)` (derive builds
   `node.input_surface` as exactly the union), so coverage holds whenever no recorded edge is dropped
   (equivalently, when dependency endpoints all resolve).
5. **Compatible monotone subsumption.** Both audits use the same monotone `σ`.

The operational conditions for applying this to a live database (snapshot consistency, endpoint
resolution, fail-closed behavior) are separate; see §6.

> **Why AV-DAG does not give CELL-DAG.** Versions `X`(cell A), `Y`(cell B), `Z`(cell A), with `Y` dep `X`
> and `Z` dep `Y`: version edges `X→Y`, `Y→Z` form a clean DAG `X→Y→Z`, but A produced both `X`(early) and
> `Z`(late), so contraction yields `A→B` (from `X→Y`) and `B→A` (from `Y→Z`) — a block cycle. The enabling
> condition is one block credited with versions at two separated times, with another block's work between.
> CELL-DAG is a property of `β`, independent of `G` being a DAG, so it must be checked, not inferred.

## 3. Theorem and proof

Define the typed finding sets (note the different domains):
```
M_V = { (v, a) : |Vin(v)(a)| ≥ 2 }        (version-level findings)
M_B = { (p, a) : |Base(p)(a)| ≥ 2 }       (block-level findings)
```

> **Theorem (Preservation).** Under conditions 1–5, `{ (β(v), a) : (v,a) ∈ M_V } ⊆ M_B`. That is, every
> artifact-level mixture at a version maps to a mixture at that version's producer block.

**Lemma (Containment).** Under 1–5, for every `v ∈ V`: `Vin(v) ⊆ Base(β(v))` (pointwise), and
`Vout(v) ⊆ Cout(v)`.

*Proof.* Outer strong induction over blocks in a `G/β` topological order (producers first, by CELL-DAG).
Fix block `p`; assume the Lemma for vertices of strictly earlier blocks. Take `v ∈ O(p)`. Monotone `σ_v`,
applied identically, reduces the goal to **(⋆) `Base(p) ⊇ Vin(v)`**. Since `Vin(v) = ⋁_{u∈In(v)} Vout(u)` and
`In(v) ⊆ I(p)`, (⋆) follows from **(⋆⋆): for every `u ∈ I(p)`, `Base(p) ⊇ Vout(u)`** (target `Base(p)`,
not `Seed_p(u)`; any extra inputs coverage allows in `S(p)\I(p)` only enlarge `Base(p)`). Prove (⋆⋆) by
inner well-founded induction on `u` along `G`:

- **`u` exogenous** (`β(u)=s_u`, `In(u)=∅`). The edge `u→v` gives `s_u → p`, so `s_u` precedes `p`;
  `Base(s_u)=∅`, `Cout(u)=σ_u(∅)={α(u):{u}}=Vout(u)=Seed_p(u) ⊆ Base(p)`. ✓
- **`u` cross-block** (`β(u)=q≠p`, real). The edge `u→v` gives `q→p`, so by CELL-DAG `q` precedes `p`;
  `Seed_p(u)=Cout(u)=σ_u(Base(q))`. Outer IH: `Base(q) ⊇ Vin(u)`; monotonicity: `Cout(u) ⊇ Vout(u)`; and
  `Seed_p(u) ⊆ Base(p)`. ✓ *(Only step using CELL-DAG.)*
- **`u` intra-block** (`β(u)=p`). `Seed_p(u)={α(u):{u}}` is weaker, so route through the union:
  `In(u) ⊆ I(p) ⊆ S(p)`, each `w∈In(u)` a strict `G`-predecessor, so inner IH gives `Base(p) ⊇ Vout(w)`;
  join ⟹ `Base(p) ⊇ Vin(u)`; then `σ_u` sets `α(u)→{u} ⊆ Base(p)` (via `Seed_p(u)`), others unchanged. ✓

Join (⋆⋆) over `In(v)` ⟹ (⋆). ∎ **Theorem:** `(v,a)∈M_V` ⟹ `|Vin(v)(a)|≥2` ⟹ (by ⋆) `|Base(β(v))(a)|≥2`
⟹ `(β(v),a)∈M_B`. ∎

A zero-input real output is handled with no special case: `Vin(v)=∅ ⊆ Base(p)` trivially, and it stays in
its cell (`β(v)=p`), contributing to `Base(p)` only if a sibling records a dependency on it.

> **Proposition (Strictness).** One block `p`, outputs `o1,o2`, `In(o1)={x1}`, `In(o2)={x2}`,
> `α(x1)=α(x2)=a`: `(o1,a),(o2,a) ∉ M_V` (neither output mixes), yet `Base(p)(a)={x1,x2}` so
> `(p,a) ∈ M_B`. The inclusion of the Theorem is therefore **proper for this graph**. "Superset" is a
> statement about the class of valid graphs; a particular project may produce identical version- and
> block-level findings.

## 4. Why both DAG conditions are required

The recurrences are defined only on the DAGs: a `G/β` topological order exists only under CELL-DAG, and
the inner induction's well-foundedness needs AV-DAG. Outside either condition the abstract audit is
undefined and the theorem makes no claim — it is not that a cyclic graph is proven to violate
preservation, but that the model does not apply. AV-DAG and CELL-DAG are independent (`G/β` can be
cyclic while `G` is a DAG, per the box; and an intra-block version cycle is a dropped self-loop, so `G`
can be cyclic while `G/β` is a DAG), so both are required. §5 describes what the *implementation* does
when a graph violates these conditions.

## 5. Implementation behavior outside the DAG conditions (not part of the theorem)

The gate does not currently verify the DAG conditions; it runs a single-pass audit whose ordering step
degrades rather than errors. On a producer-block cycle (CELL-DAG violated), `core/audit._toposort` runs
Kahn's algorithm, which stalls, and then appends the residual blocks in id order. When a block is
processed before a cross-block input's producer, that input's cone is unavailable and is seeded as a
singleton `{α(u):{u}}`, dropping its ancestry. This is order-dependent and can produce a false negative:
a real version-level mixture is missed at the block that produced the mixed output.

*Concrete witness* (verified against the real gate). Versions `x1,x2` (artifact X, `x2` current), `r`;
`b1`(cell cB) reads `x1`; `v2`(cell cA) reads `r`; `w`(cB) reads `v2` and `b1`; `o`(cA) reads `w` and
`x2`. `G` is acyclic, but `G/β` has the cycle `cA↔cB`. Version-level: `o`'s cone reaches X at `{x1,x2}`
→ mixture. Cell-level: the fallback orders `cA` before `cB`, seeds `w` as a singleton (dropping `x1`), so
`Base(cA)[X]={x2}` and `cA` reads clean — the mixture on its own output `o` is missed. (Block `cB` is
independently flagged, so the graph does not go globally blind, but the block that produced the mixed
output under-detects.)

This is why CELL-DAG is not optional in practice, and why §6 lists validation as required hardening: a
trust gate returning a false *clean* is the worst failure. The fix is to detect the stalled order and
return an incomplete result rather than a degraded verdict.

## 6. Operational applicability, and the guards we haven't built

Applying the theorem to a live database needs, beyond the formal conditions of §2:

1. one frozen or transactionally consistent snapshot (versions, deps, producer ids, identities read from
   one logical state, not a database mid-write);
2. dependency endpoints and identities resolve in the selected closure;
3. the ordering graph contains every producer-contraction edge;
4. the capture scope is stated (the guarantee is relative to recorded edges — §7);
5. on any validation failure, fail closed.

The current gate does not enforce any of these. It does not pin a read snapshot, does not validate
AV-DAG or CELL-DAG, does not check input-surface coverage or ordering-graph completeness, and has no
`lineage_incomplete` verdict; on a violated condition it silently degrades (§5). These are required
hardening we have not built.

Therefore the theorem applies to snapshots that are separately frozen and structurally validated, not
automatically to every live gate read. What supports shipping today is empirical, not enforced: across the
snapshots inspected during development, these conditions held (no producer-block cycle and no unresolved
dependency endpoint were observed; producer retention — the cycle-enabling mechanism — occurred but never
cycled). That is supporting evidence, not a schema guarantee, and the gate does not check it at run time
yet.

## 7. What this means for the gate

Mapping the model onto the code: `V` is the artifact versions, `In` the recorded `artifact_dependencies`,
`β` the `producing_cell_id` (or a synthetic block for a producerless version), `α` the `artifact_id`; the
gate computes `Base`/`Cout` in `core/audit._cones`, and `S(p)` is a node's `input_surface`. On a validated
snapshot, two things follow.

Coarsening to the producer block costs no detection against the resolved recorded edges: the block audit
reports every mixture the per-version recorded audit would, so sensitivity under the version→block mapping
is unchanged. What it can do is flag more. By the Proposition a block reads as mixed when two of its
outputs each consumed a different version of one artifact, even though neither output mixed on its own,
and a downstream consumer of such an output inherits the pooled base and may be flagged in turn until a
later production of that artifact subsumes it. That is the intended trade: block pooling treats every
recorded input to a block as potentially relevant to each of its outputs, because within one producer
block we take the recorded per-output edges as a lower bound on true influence. That last part is the
policy behind the choice, not a premise of the theorem, which holds structurally whatever the agent did
with its inputs.

So the honest scope is narrow, and worth stating plainly. The gate loses nothing against the resolved
recorded edges, and the extra block-level flags are a conservative choice rather than a defect. It does
not tell you which output used both versions — the derived graph keeps the pooled surface, not per-output
identity, though the raw dependency rows still carry it. It does not see influence that never became a
recorded edge. And "no loss" means against the recorded graph: not a claim about all real influence, and
not a claim about recall in the abstract.

One capture behavior is worth naming, because it is easy to wave away. Mixtures are keyed on version ids,
and capture sometimes records a dependency to a checksum-equivalent version — the content source of a
byte-identical copy — rather than the version literally read. Since a version id is the unit of a mixture,
that redirection can change which id lands in a cone, which makes it a capture limitation, not a harmless
normalization. It would only be harmless under a different mixture definition based on content-equivalence
classes, which the gate does not use.
