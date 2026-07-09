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
