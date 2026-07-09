# Claude Design brief — Provenance Gate **cockpit** (m0)

You are the design lead redesigning a working operator tool: `ui/cockpit.html`, the cockpit for a
*provenance gate* over AI-agent-produced science. It's a real, live single-file web app served offline
by a local Python server (`src/provenance_gate/adapters/external/server.py`) and tested by hand against it. **Design against
the interactive mock (it auto-runs on baked fixtures with no server); the data + API layer is frozen.**

Open aesthetic latitude on *form*. What is **not** yours to change: **meaning** (§3), the **data/network
layer** (§2.1), and the **mock scaffolding** (§2.2).

---

## 1. The subject (so the identity is earned, not generic)

The gate sits between an **AI agent running analyses in Claude Science** and the **scientist** who must
trust the result. It observes Claude Science **read-only** and maps the **provenance DAG**: each node is a
computation (or a raw *source*); edges are artifact consumption; every artifact version is pinned by
**checksum**. Later rounds add trust verdicts; **m0 is structure only — just nodes and edges**.

Register: **forensic instrument / lab console / flight cockpit**, not a consumer dashboard — restrained,
dense, data-first, state readable at a glance. Native vocabulary — node, source vs computation, artifact
version, checksum, consumes, lineage — is where distinctive, non-templated choices come from. "Observe,
don't touch" is itself a trust cue. It is a **UI, not a document**: scanned and operated. Real care for the
**data typography** (filenames, checksums, version numbers, code) — that mono/tabular content is most of the screen.

---

## 2. Hard constraints (breaking any of these breaks the tool)

1. **The data + observability layer is frozen — the `// ===== PG:DATA-IO … =====` and
   `// ===== PG:LOG … =====` fenced blocks.** `PG:DATA-IO` defines `getProjects()` / `getGraph(pid)` (the
   real `fetch` calls) and the fields the code reads (Appendix A); `PG:LOG` is client observability (it
   records fetch/action/error events). You may restructure the DOM/CSS and reorganize the *presentation* JS
   freely, but do **not** rename, drop, restructure, or restyle anything inside either block; keep calling
   `getProjects()`/`getGraph()` for data. If a redesign seems to need a new endpoint/field, **flag it — don't
   add it.** *Optional but encouraged:* call **`PG.action(name, detail)`** at the **meaningful** interaction
   points — project switch, node select, opening the inspector — with an identifying `detail` (e.g. the node
   id). **Skip ephemeral view ops** (fit, pan, zoom, hover): they change nothing and only add log noise.
2. **The mock scaffolding is ours — the `// ===== PG:MOCK … =====` fenced block. Do not touch it.** It's
   generated, inert on the real server, and **regenerated on our side each build**. Never restyle, extend,
   or remove it, and never make real UI logic depend on it. Design *around* it; it riding along in what you
   hand back is fine — we re-bake.
3. **Offline & self-contained.** All app CSS/JS inlined; the DAG library rides as a **project-local file**
   (below), not an external dependency. **No CDN, no external stylesheet, no remote font, no network fetch for
   assets.** Custom font → `@font-face` data-URI; else a considered system-font stack. **The DAG engine is
   provided** as the attached `cytoscape-dagre.bundle.min.js` (Cytoscape.js + dagre + cytoscape-dagre), already
   referenced via `<script src="./cytoscape-dagre.bundle.min.js">` in `<head>` — `window.cytoscape` is global
   and the `dagre` layout is registered. Use it; do not fetch a CDN copy, re-add, or inline it.
4. **A working tool at every hand-off.** Keep the interactions in §4 + Appendix A intact.

---

## 3. Semantic invariants — information, not decoration (must survive the rethink)

1. **Node `kind`: `source` vs `computation`.** A *source* is a raw input / provenance root (no producing
   cell); a *computation* is derived. Must read as different in form, not just label.
2. **The verdict rail (forward hook).** Reserve a per-node **status slot** (a rail/stripe). In m0 every node
   is **unevaluated (neutral)** — trust verdicts (green/amber/red) land later. Design the slot so color can
   light up later **without relayout**. Don't invent states now.
3. **Frozen artifact = checksum pin.** A filename + short checksum is the *pin* everything hangs off — the
   core trust content. Give that mono/tabular typography genuine care.
4. **Read-only stance.** The tool observes Claude Science and never mutates it — a calm, persistent trust cue.
5. **Frames group cells (structural, not trust).** Cells sharing a CS *frame* (a task) belong together; draw the
   frame as a **bounding container** titled by the frame's task message, with its cells inside. Frames carry
   **no verdict** and never enter trust — grouping only. A cell's own label is just **`cell N`**; the *frame*
   holds the descriptive task text, so cells don't repeat it.
6. **Artifact version currency (information, not decoration).** Each artifact ref carries its `version_number`
   and whether it's the **current** version (`is_latest`). A **non-current (stale) version is real trust
   content**: show the version on the chip, make a stale one read as visually distinct, and note the current
   version (`latest_version_number`). This is the same currency signal the merge/lineage audit uses — not a
   verdict (that rail stays neutral in m0), just factual "which version, is it current".

---

## 4. Theme & palette — match Claude Science (native light + dark)

The cockpit should read as **part of Claude Science**, which ships **both a light and a dark theme** (it
switches natively). **Match its aesthetic in both.** A live CS screenshot (light mode) is provided as the
reference — pull the light cues from it directly:

- **Light (from the CS screenshot):** clean white / faint off-white ground, **near-black primary text**,
  **muted-gray secondary** text, **subtle light-gray borders/dividers**, generous whitespace, minimal color.
  Sans-serif for prose; **monospace for all data** (filenames, values, checksums, code), often set in a
  quiet chip/pill. Checks/status in muted tones, not loud accents. Restrained and precise — a lab instrument,
  but *light and calm* like CS, **not a dark console**.
- **Dark:** CS's dark counterpart — same restraint, inverted. (A dark-mode CS screenshot can follow; until
  then, derive a faithful dark of the same palette.)
- **Build both from the first component**, via **CSS custom properties**: every color a variable in a
  `:root` (light) set + a dark override (`@media (prefers-color-scheme: dark)` and/or a `[data-theme]`
  attribute — the switch trigger is our plumbing). Reference only variables thereafter. Keep semantic status
  color separate from any accent.

---

## 5. Components — prioritized

### P1 — The DAG map (flagship) · Cytoscape + dagre
- **Today:** a hand-rolled SVG with a naive depth-column `layout()`; plain rects, tiny text; no pan/zoom/fit;
  doesn't scale past a couple dozen nodes.
- **Good:** a real **layered** DAG via **Cytoscape.js + cytoscape-dagre** — **provided as the attached bundle**
  (see §2; `window.cytoscape` global, `dagre` layout registered). **Use it — don't fetch or re-implement.**
  A layered left→right layout (`{ name: 'dagre', rankDir: 'LR' }` is a fine start).
  - **Frame bounding containers (new).** Cells sharing a `cs_frame_id` are grouped inside a **container node**
    titled by the frame's `label` (from `frames[]`) — Cytoscape **compound nodes** (`data.parent = <frameId>`;
    dagre lays out compound graphs). Frames are **structural only** — no verdict, no trust.
  - **Compact node cards.** The node label is now **`cell N`** for a computation (a source shows its filename);
    the *frame container* carries the task message, so cells don't repeat it. Card = label + a **`kind` badge**
    (source/computation) + output artifact chips (each = **filename + version**; a **stale** version reads
    distinct, with the current version noted) + the neutral **verdict rail**. (Node contents + edge styling
    iterate later — compact is fine.)
  Interactions:
  - **Pan / zoom**, and **fit-to-view that accounts only for the graph-view panel** — when the node inspector
    opens, the graph panel **shrinks and re-fits** to the remaining area.
  - **Fit is a sticky toggle:** double-click empty canvas toggles auto-fit **stick ↔ unstick**.
  - **Single-click a node → select it and open the node inspector** (§P2). **Click empty canvas / deselect →
    close the inspector.**
  - **Double-click a node → select and zoom to it.**
  - Hover a node → highlight its lineage (predecessors/successors), dim the rest.
- **Data:** `GET /api/graph` → `nodes[]` (`id, kind, label, cs_frame_id, input_surface[], output_surface[]`),
  `edges[]` (`src_node_id, dst_node_id, reference_name`), and **`frames[]`** (`id, label`) — group nodes into
  containers by matching `node.cs_frame_id` to a `frame.id`. Appendix A.

### P2 — The node inspector = a collapsible side panel
- **Good:** a side panel that **slides in on node select and out on deselect**; a **collapse button on the
  panel header** does the same as deselect. Opening it **shrinks the graph panel** (which re-fits). Contents
  for m0 are basic — **identity** (node id, cs cell/frame id, cell index) and the node's **inputs/outputs** as
  artifact rows (*filename · version · checksum pin · revision link*), plus the producing **code**. (What
  else goes in, and its layout, we iterate later — for now, the plain fields from the mock data.)
- **Data:** the node's own fields from `/api/graph` — no new endpoint.

### P3 — The header / status
- **Good:** a glanceable **mono status header** — project picker, a **live indicator** (the 2s poll + snapshot
  age from `built_at`), node/edge counts as a scannable summary, a legend (source · computation), and the
  "reads CS read-only" cue stated plainly.
- **Data:** `GET /api/projects` → `[{id, name}]`; counts + `built_at` from `/api/graph`.

---

## 6. Process

Per component, before code: a short **design plan** — 4–6 named palette hexes (light + dark), a display /
body / mono-data type role, and a 1–2 sentence layout concept — derived from the forensic-instrument subject
and CS's palette, deliberately *not* the generic AI-dark-dashboard default. Then build to the plan, theme-aware.
Take one real, subject-appropriate risk where it serves legibility; keep the dense content quiet around it.

---

## 7. Hand-off workflow — design against the mock

We hand you **`cockpit.mock.html`** — the clean `ui/cockpit.html` with a baked mock block (built by
`design/build_mock.py` from real snapshots in `design/graph_fixtures.json`). It's **dual-mode**:

- **In your sandbox (no server):** `getProjects()`/`getGraph()` auto-fall-back to the baked fixtures and a
  **◆ MOCK DATA** badge appears. The **project dropdown switches scenarios** — the fixtures include a **large
  DAG (~83 nodes, several frames)**, a **small one (4 cells in one frame)**, a **mid one**, an **empty
  project**, and a **multi-version project** (artifacts at several versions, some **stale** vs current) — so you
  can design every state, including **frame containers** and the **version/stale chips**, with no backend.
- **On our server:** the same calls hit the real API — the mock is inert.

Design the cockpit **around** the two fenced blocks (§2). Deliver **one self-contained file**; the scaffolding
riding along is fine — on our side we run `build_mock.py strip` to recover a clean `ui/cockpit.html`, serve it
against the live gate, and review your component with real Claude Science data.

---

## Appendix A — Frozen API / data contract (do not break)

Same-origin `fetch`, `GET`, wrapped by `getProjects()` / `getGraph()` in the PG:DATA-IO block.

- `getProjects()` → `GET /api/projects` → `[{ id, name }]`
- `getGraph(pid)` → `GET /api/graph?project=<id>` → `{ cs_project_id, built_at, nodes:[…], edges:[…], frames:[…] }`
  - **node**: `{ id, cs_project_id, kind ('source'|'computation'), label, input_surface:[ArtifactRef],
    output_surface:[ArtifactRef], cs_frame_id, cs_cell_id, cell_index, code }` — `label` is **`cell N`** for a
    computation, the **filename** for a source (the *frame* holds the task message); `cs_frame_id` links the node
    to its frame.
  - **ArtifactRef**: `{ artifact_version_id, artifact_id, version_number, filename, checksum, storage_path,
    parent_version_id, kind, is_latest, latest_version_id, latest_version_number }` — `is_latest` = whether this
    is its artifact's **current** version; `latest_version_id` / `latest_version_number` point to that current
    version (they equal this ref when `is_latest` is true).
  - **edge**: `{ id, src_node_id, dst_node_id, via_artifact_version_id, reference_name }`
  - **frame**: `{ id, label, parent_frame_id, kind }` — a CS task grouping cells; draw as a bounding container
    titled by `label`, with member nodes (those whose `cs_frame_id == id`) inside. Structural only — no trust.
- **Keep working:** project switch (re-fetch graph for the selected id), node-click → inspector from the
  node's own fields, the 2-second poll (re-fetch `/api/graph`). **`getGraph()` returns `null` when nothing
  changed since the last poll — skip the re-render then; only re-render when you get a real graph.**

> m0 is structure only — no write endpoints, no verdict/assumption/version fields yet. Those arrive later and
> extend `/api/graph` **additively**; design the verdict rail + inspector with room to grow, but don't invent
> those fields now.
