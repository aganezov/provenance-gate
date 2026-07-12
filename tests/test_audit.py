"""core.audit — the first trust verdict, over hand-built Graphs (pure, no CS).

Covers the two verdicts and the revision edge case we care most about: a linear revision chain
must NOT read as a mix, and the revising cell must NOT read as stale.
"""

import pytest

from provenance_gate.core import audit
from provenance_gate.core.model import Graph, Node


def ref(vid, aid, num, current, is_latest, filename):
    from provenance_gate.core.model import ArtifactRef
    return ArtifactRef(
        artifact_version_id=vid, artifact_id=aid, version_number=num, filename=filename,
        checksum="c", storage_path="p", is_latest=is_latest,
        latest_version_id=f"{aid}_v{current}", latest_version_number=current,
    )


def node(nid, inputs=(), outputs=(), kind="computation"):
    return Node(id=nid, cs_project_id="p", kind=kind, label=nid,
                input_surface=tuple(inputs), output_surface=tuple(outputs))


def test_clean_linear_is_all_clean():
    raw = ref("rv1", "R", 1, 1, True, "raw.csv")
    xv1 = ref("xv1", "X", 1, 1, True, "x.csv")
    yv1 = ref("yv1", "Y", 1, 1, True, "y.csv")
    src = node("src", outputs=[raw], kind="source")
    c1 = node("c1", inputs=[raw], outputs=[xv1])
    c2 = node("c2", inputs=[xv1], outputs=[yv1])
    v = audit.audit_graph(Graph(cs_project_id="p", nodes=(src, c1, c2)))
    assert {k: r.level for k, r in v.items()} == {
        "src": audit.CLEAN, "c1": audit.CLEAN, "c2": audit.CLEAN
    }


def test_stale_input_is_flagged():
    xv1 = ref("xv1", "X", 1, 2, False, "x.csv")   # v1 while current is v2 -> stale
    src = node("src", outputs=[xv1], kind="source")
    c2 = node("c2", inputs=[xv1], outputs=[ref("yv1", "Y", 1, 1, True, "y.csv")])
    v = audit.audit_graph(Graph(cs_project_id="p", nodes=(src, c2)))
    assert v["c2"].level == audit.STALE_INPUT
    (issue,) = v["c2"].stale
    assert issue.artifact == "x.csv" and issue.versions == (1,) and issue.current == 2


def test_revising_cell_is_not_stale():
    # cB reads X v1 (non-current) AND writes X v2 — a revision, not stale use
    xv1 = ref("xv1", "X", 1, 2, False, "x.csv")
    xv2 = ref("xv2", "X", 2, 2, True, "x.csv")
    src = node("src", outputs=[xv1], kind="source")
    cB = node("cB", inputs=[xv1], outputs=[xv2])
    v = audit.audit_graph(Graph(cs_project_id="p", nodes=(src, cB)))
    assert v["cB"].level == audit.CLEAN


def test_linear_revision_downstream_is_clean():
    # src: X v1 ; cB revises X v1 -> X v2 ; cC reads X v2 -> Y ; report reads Y. No mix.
    xv1 = ref("xv1", "X", 1, 2, False, "x.csv")
    xv2 = ref("xv2", "X", 2, 2, True, "x.csv")
    yv1 = ref("yv1", "Y", 1, 1, True, "y.csv")
    src = node("src", outputs=[xv1], kind="source")
    cB = node("cB", inputs=[xv1], outputs=[xv2])
    cC = node("cC", inputs=[xv2], outputs=[yv1])
    report = node("report", inputs=[yv1], outputs=[ref("zv1", "Z", 1, 1, True, "report.md")])
    v = audit.audit_graph(Graph(cs_project_id="p", nodes=(src, cB, cC, report)))
    assert v["cC"].level == audit.CLEAN and v["report"].level == audit.CLEAN


def _divergent_nodes():
    # X v1 -> cX -> A (uses OLD X) ; X v1 -> cB -> X v2 -> cY -> B (uses NEW X)
    xv1 = ref("xv1", "X", 1, 2, False, "x.csv")
    xv2 = ref("xv2", "X", 2, 2, True, "x.csv")
    av1 = ref("av1", "A", 1, 1, True, "a.csv")
    bv1 = ref("bv1", "B", 1, 1, True, "b.csv")
    src = node("src", outputs=[xv1], kind="source")
    cB = node("cB", inputs=[xv1], outputs=[xv2])
    cX = node("cX", inputs=[xv1], outputs=[av1])
    cY = node("cY", inputs=[xv2], outputs=[bv1])
    return src, cB, cX, cY, av1, bv1


def test_version_mix_on_divergent_branches():
    src, cB, cX, cY, av1, bv1 = _divergent_nodes()
    report = node("report", inputs=[av1, bv1], outputs=[ref("rv1", "R", 1, 1, True, "report.md")])
    v = audit.audit_graph(Graph(cs_project_id="p", nodes=(src, cB, cX, cY, report)))
    assert v["report"].level == audit.VERSION_MIX
    (issue,) = v["report"].mixed
    assert issue.artifact == "x.csv" and set(issue.versions) == {1, 2}
    assert v["cX"].level == audit.STALE_INPUT   # uses old X, doesn't revise it
    assert v["cB"].level == audit.CLEAN         # revises X, not stale


def test_co_output_sibling_does_not_fake_a_mix():
    # THE regression: cA produces x_a AND qc v1; cB produces x_b AND qc v2 (co-outputs). cC consumes
    # x_a + x_b ONLY — never qc. The divergent qc are siblings of cC's ancestors, off its
    # consumption path, so they must NOT reconverge into a version_mix at cC. (Holds whether or not
    # qc v1->v2 are linearly related — the audit judges consumption, not co-production.)
    xa = ref("xa", "XA", 1, 1, True, "x_a.csv")
    xb = ref("xb", "XB", 1, 1, True, "x_b.csv")
    qc1 = ref("qc1", "QC", 1, 2, False, "qc.csv")   # qc head is v2; v1 is non-current
    qc2 = ref("qc2", "QC", 2, 2, True, "qc.csv")
    cA = node("cA", outputs=[xa, qc1])              # x_a and qc v1 are SIBLING outputs of one cell
    cB = node("cB", outputs=[xb, qc2])
    cC = node("cC", inputs=[xa, xb], outputs=[ref("rep", "R", 1, 1, True, "report.csv")])
    v = audit.audit_graph(Graph(cs_project_id="p", nodes=(cA, cB, cC)))
    assert v["cC"].level == audit.CLEAN             # consumed only x_a/x_b -> no qc mix
    assert v["cA"].level == audit.CLEAN and v["cB"].level == audit.CLEAN


def test_two_versions_from_one_cell_do_not_fake_a_mix():
    # a single cell writes qc v1 AND qc v2 (a revision) plus x.csv; a downstream cell
    # reads only x.csv. The two qc versions on the surface must not reconverge into a mix
    # for a consumer that never read qc — the per-version cone keeps each output's lineage apart.
    # The result is independent of output_surface order (old subsumption was order-sensitive).
    qc1 = ref("qc1", "QC", 1, 2, False, "qc.csv")
    qc2 = ref("qc2", "QC", 2, 2, True, "qc.csv")
    x = ref("x", "X", 1, 1, True, "x.csv")
    c0 = node("c0", outputs=[qc1, qc2, x])
    d = node("d", inputs=[x], outputs=[ref("y", "Y", 1, 1, True, "y.csv")])
    v = audit.audit_graph(Graph(cs_project_id="p", nodes=(c0, d)))
    assert v["c0"].level == audit.CLEAN and v["d"].level == audit.CLEAN


def test_audit_inputs_ignores_co_output_siblings():
    # the pre-write path (audit_inputs) must judge consumed lineage too: a planned node reading
    # x_a + x_b whose producers ALSO emitted divergent qc siblings is clean: qc was never consumed.
    xa = ref("xa", "XA", 1, 1, True, "x_a.csv")
    xb = ref("xb", "XB", 1, 1, True, "x_b.csv")
    cA = node("cA", outputs=[xa, ref("qc1", "QC", 1, 2, False, "qc.csv")])
    cB = node("cB", outputs=[xb, ref("qc2", "QC", 2, 2, True, "qc.csv")])
    g = Graph(cs_project_id="p", nodes=(cA, cB))
    assert audit.audit_inputs(g, ["xa", "xb"]).level == audit.CLEAN


def test_external_input_still_counts_toward_a_mix():
    # A1: rep reads an EXTERNAL X v1 (produced by no node in the graph) plus Y (built on X v2). X
    # reconverges at v1 (direct) and v2 (via Y); the external version must be seeded
    # from its own ref (not dropped) AND nameable (ref_map indexes inputs too), not downgraded.
    xv1 = ref("xv1", "X", 1, 2, False, "x.csv")   # external, non-current
    xv2 = ref("xv2", "X", 2, 2, True, "x.csv")
    yv1 = ref("yv1", "Y", 1, 1, True, "y.csv")
    sx = node("s_x", outputs=[xv2], kind="source")
    cy = node("cy", inputs=[xv2], outputs=[yv1])
    rep = node("rep", inputs=[xv1, yv1], outputs=[ref("rp", "R", 1, 1, True, "report.csv")])
    v = audit.audit_graph(Graph(cs_project_id="p", nodes=(sx, cy, rep)))
    assert v["rep"].level == audit.VERSION_MIX
    (m,) = v["rep"].mixed
    assert m.artifact == "x.csv" and set(m.versions) == {1, 2}


def test_mix_issue_fields_are_deterministic_not_set_order():
    # A2: two versions of one artifact_id carrying DIFFERENT filenames (a rename) reconverge at rep.
    # The issue's filename must be a deterministic pick (lead = min version id), not set-iteration
    # order, so an identical graph audits identically across runs (hash-randomized set iteration).
    a1 = ref("a_id1", "A", 1, 2, False, "a.csv")        # version id 'a_id1'
    a2 = ref("a_id2", "A", 2, 2, True, "renamed.csv")   # same artifact_id 'A', renamed file
    rep = node("rep", inputs=[a1, a2], outputs=[ref("rp", "R", 1, 1, True, "r.csv")])
    v = audit.audit_graph(Graph(cs_project_id="p", nodes=(
        node("sA1", outputs=[a1], kind="source"), node("sA2", outputs=[a2], kind="source"), rep)))
    (m,) = v["rep"].mixed
    assert m.artifact == "a.csv"   # 'a_id1' < 'a_id2' -> lead a1 -> 'a.csv', every run


def test_null_version_numbers_still_flag_a_mix():
    # A3: two distinct versions of artifact Q both with version_number=None reconverge at rep. Two
    # distinct version ids ARE a mix regardless of numbers -- must not slip through as clean.
    qa = ref("qa", "Q", None, None, True, "q.csv")
    qb = ref("qb", "Q", None, None, True, "q.csv")
    rep = node("rep", inputs=[qa, qb], outputs=[ref("rp", "R", 1, 1, True, "r.csv")])
    v = audit.audit_graph(Graph(cs_project_id="p", nodes=(
        node("sA", outputs=[qa], kind="source"), node("sB", outputs=[qb], kind="source"), rep)))
    assert v["rep"].level == audit.VERSION_MIX and v["rep"].mixed[0].artifact == "q.csv"
    assert v["rep"].mixed[0].versions == ()   # all NULL numbers -> empty versions, still flagged


def test_revised_input_and_co_output_from_old_version_flags_downstream_mix():
    # Finder B: cell cK revises X v1->v2 AND co-emits Y from OLD v1; a downstream reading BOTH the
    # revised X v2 and Y (on v1) genuinely mixes X. Old per-cell subsumption returned clean (it
    # collapsed X to v2 for ALL cK's outputs); the per-version cone keeps Y on v1 and catches it.
    xv1 = ref("xv1", "X", 1, 2, False, "x.csv")
    xv2 = ref("xv2", "X", 2, 2, True, "x.csv")
    yv1 = ref("yv1", "Y", 1, 1, True, "y.csv")
    src = node("src", outputs=[xv1], kind="source")
    cK = node("cK", inputs=[xv1], outputs=[xv2, yv1])   # revises X to v2 AND co-emits Y from v1
    rep = node("rep", inputs=[xv2, yv1], outputs=[ref("rp", "R", 1, 1, True, "report.csv")])
    v = audit.audit_graph(Graph(cs_project_id="p", nodes=(src, cK, rep)))
    assert v["rep"].level == audit.VERSION_MIX
    (m,) = v["rep"].mixed
    assert m.artifact == "x.csv" and set(m.versions) == {1, 2}


def test_audit_inputs_hypothetical_planned_write():
    # audit a PLANNED node reading a.csv + b.csv before it exists — the skill's pre-write case
    src, cB, cX, cY, av1, bv1 = _divergent_nodes()
    verdict = audit.audit_inputs(Graph(cs_project_id="p", nodes=(src, cB, cX, cY)), ["av1", "bv1"])
    assert verdict.level == audit.VERSION_MIX and set(verdict.mixed[0].versions) == {1, 2}


def test_audit_inputs_raises_on_unknown_id():
    # PR #6 review: an id not in the graph would silently drop -> false CLEAN; raise instead
    src, cB, cX, cY, av1, bv1 = _divergent_nodes()
    g = Graph(cs_project_id="p", nodes=(src, cB, cX, cY))
    with pytest.raises(ValueError):
        audit.audit_inputs(g, ["av1", "nope"])


def test_audit_survives_a_cycle():
    # PR #6 review: a corrupt (cyclic) graph must degrade, not KeyError in the cone pass
    aout = ref("aout", "A", 1, 1, True, "a.csv")
    bout = ref("bout", "B", 1, 1, True, "b.csv")
    nA = node("nA", inputs=[bout], outputs=[aout])
    nB = node("nB", inputs=[aout], outputs=[bout])   # A <-> B cycle
    v = audit.audit_graph(Graph(cs_project_id="p", nodes=(nA, nB)))
    assert set(v) == {"nA", "nB"}   # a verdict per node, no crash


def test_stale_issues_sorted_by_artifact():
    # PR #6 review: stale[] must be deterministically ordered (by filename), not input order
    zst = ref("zst", "Z", 1, 2, False, "z.csv")
    ast = ref("ast", "A", 1, 2, False, "a.csv")
    sZ = node("sZ", outputs=[zst], kind="source")
    sA = node("sA", outputs=[ast], kind="source")
    c = node("c", inputs=[zst, ast], outputs=[ref("out", "O", 1, 1, True, "o.csv")])
    v = audit.audit_graph(Graph(cs_project_id="p", nodes=(sZ, sA, c)))
    assert v["c"].level == audit.STALE_INPUT
    assert [i.artifact for i in v["c"].stale] == ["a.csv", "z.csv"]


def test_graph_response_wire_shape():
    # the ONE getGraph serializer both the server and the in-CS kernel call: asdict(graph) + a
    # per-node verdict. Pinning it here is what stops the two surfaces drifting.
    raw = ref("rv1", "R", 1, 1, True, "raw.csv")
    xv1 = ref("xv1", "X", 1, 1, True, "x.csv")
    g = Graph(cs_project_id="p", built_at=1.0,
              nodes=(node("src", outputs=(raw,), kind="source"),
                     node("c1", inputs=(raw,), outputs=(xv1,))))
    resp = audit.graph_response(g)
    assert set(resp) >= {"cs_project_id", "nodes", "edges", "frames", "built_at"}   # graph keys
    assert {nd["id"] for nd in resp["nodes"]} == {"src", "c1"}
    verdicts = audit.audit_graph(g)
    for nd in resp["nodes"]:
        assert set(nd["verdict"]) == {"level", "stale", "mixed"}         # Verdict asdict shape
        assert nd["verdict"]["level"] == verdicts[nd["id"]].level        # matches audit_graph
