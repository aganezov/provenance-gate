# Auditing a captured rollout

`project.db` is a project-scoped operon snapshot — the frozen final state of one PBMC
figure-package rollout produced by the generation harness. In this run the shipped package
assembled Panel B (strict QC, `cells.qc.csv` v2) alongside Panels A, C, and D (baseline QC,
`cells.qc.csv` v1), so the final figure combines two cell populations.

The gate reads the frozen snapshot directly — no live Claude Science — and names the conflict in
one command:

```
$ uv run pg-audit evals/claude-science-rollouts/examples/pbmc-version-mix/project.db
proj_82d89c89545d: 12 cells, 6 flagged
  [stale_input] cells.qc.csv
                stale: qc_params.csv (v1; current v2)
  [stale_input] cytotoxic_signature.csv, qc_summary.csv, ifn_signature.csv, composition.csv
                stale: cells.qc.csv (v1; current v2)
                stale: qc_params.csv (v1; current v2)
  [version_mix] figure_final.png
                mix: cells.qc.csv (v1/v2; current v2)
                mix: qc_params.csv (v1/v2; current v2)
  [stale_input] figure_style.csv, figure_manifest.csv
                stale: panel_ifn.csv (v1; current v2)
  [version_mix] figure_values_final.csv, figure_manifest_final.csv
                mix: cells.qc.csv (v1/v2; current v2)
                mix: qc_params.csv (v1/v2; current v2)
  [stale_input] panel_qc.csv, panel_ifn.csv, panel_cytotoxic.csv, panel_composition.csv
                stale: cells.qc.csv (v1; current v2)
                stale: ifn_signature.csv (v1; current v2)
```

The two `version_mix` verdicts land on the shipped paper files — `figure_values_final.csv`,
`figure_manifest_final.csv`, and the `figure_final.png` they describe — because their lineage
reaches `cells.qc.csv` at both v1 and v2. The conflict is a structural fact in the provenance
graph, so the gate names it whether or not the prose mentions it. This is the harness and the gate
meeting end to end: a real rollout captured to a db, then audited in a single line.
