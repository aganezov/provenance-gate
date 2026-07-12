"""core.subgraph.induced_subgraph: restrict a derived Graph to a chosen node set.

Built over derive_graph (a fork: raw -> c1/c2 -> c3), so we exercise the real Node/Edge model. The
central case is inducing DOWN to the fork while dropping the raw source — only edges with both
endpoints kept survive, which is what lets a selective review exclude a trusted trunk.
"""

from provenance_gate.core import derive
from provenance_gate.core.subgraph import induced_subgraph


def _v(vid, aid, num, cell, fn):
    return {"id": vid, "artifact_id": aid, "version_number": num, "checksum": "cs",
            "storage_path": "p/" + vid, "parent_version_id": None,
            "producing_cell_id": cell, "frame_id": "f1" if cell else None, "filename": fn,
            "latest_version_id": vid, "latest_version_number": num}


def _cell(cid, idx):
    return {"id": cid, "frame_id": "f1", "cell_index": idx, "source": "code"}


_FRAME = [{"id": "f1", "task_summary": "t", "name": None, "parent_frame_id": None}]


def _fork_graph():
    # raw (source) -> c1 (a.csv), raw -> c2 (b.csv); c1,c2 -> c3 (report.csv). A source + 3 cells.
    versions = {
        "raw": _v("raw", "a_raw", 1, None, "raw.csv"),
        "a": _v("a", "a_a", 1, "c1", "a.csv"),
        "b": _v("b", "a_b", 1, "c2", "b.csv"),
        "rep": _v("rep", "a_rep", 1, "c3", "report.csv"),
    }
    deps = [
        {"consumer_v": "a", "input_v": "raw", "reference_name": "raw.csv"},
        {"consumer_v": "b", "input_v": "raw", "reference_name": "raw.csv"},
        {"consumer_v": "rep", "input_v": "a", "reference_name": "a.csv"},
        {"consumer_v": "rep", "input_v": "b", "reference_name": "b.csv"},
    ]
    cells = {"c1": _cell("c1", 1), "c2": _cell("c2", 2), "c3": _cell("c3", 3)}
    return derive.derive_graph("p", versions, deps, cells, _FRAME, built_at=1.0)


def test_induces_to_subset_and_keeps_only_internal_edges():
    sub = induced_subgraph(_fork_graph(), {"c1", "c2", "c3"})   # exclude the raw source
    assert {n.id for n in sub.nodes} == {"c1", "c2", "c3"}
    # only edges among kept nodes survive: c1->c3, c2->c3; raw->c1, raw->c2 dropped
    assert {(e.src_node_id, e.dst_node_id) for e in sub.edges} == {("c1", "c3"), ("c2", "c3")}


def test_empty_keep_is_empty_graph_same_project():
    g = _fork_graph()
    sub = induced_subgraph(g, set())
    assert sub.nodes == () and sub.edges == () and sub.frames == ()
    assert sub.cs_project_id == "p" and sub.built_at == g.built_at   # identity preserved


def test_keep_all_preserves_nodes_and_edges():
    g = _fork_graph()
    sub = induced_subgraph(g, {n.id for n in g.nodes})
    assert sub.nodes == g.nodes and sub.edges == g.edges


def test_unknown_ids_ignored_and_isolated_node_has_no_edges():
    sub = induced_subgraph(_fork_graph(), {"c3", "ghost"})
    assert {n.id for n in sub.nodes} == {"c3"}   # ghost ignored
    assert sub.edges == ()                        # c3 alone -> no internal edge survives


def test_frames_narrow_to_kept_nodes():
    g = _fork_graph()
    assert {f.id for f in induced_subgraph(g, {"c1"}).frames} == {"f1"}   # c1 sits in f1
    assert induced_subgraph(g, {"raw"}).frames == ()   # the source has no frame -> none kept


def test_is_deterministic_preserves_source_order():
    g = _fork_graph()
    a = induced_subgraph(g, {"c1", "c3", "c2"})
    b = induced_subgraph(g, {"c1", "c2", "c3"})
    assert a.nodes == b.nodes and a.edges == b.edges   # order from graph, not the keep set
    assert [n.id for n in a.nodes] == [n.id for n in g.nodes if n.id in {"c1", "c2", "c3"}]
