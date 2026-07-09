"""substrate.read_project_graph derives the right nodes/edges from operon-shaped rows.

The operon-shaped fixture (`cs_conn`) lives in conftest.py and is shared with the service test.
"""

from provenance_gate.model import Frame
from provenance_gate.substrate import list_projects, read_project_graph


def test_list_projects_newest_first(cs_conn):
    assert [p["id"] for p in list_projects(cs_conn)] == ["proj_smoke", "proj_upload"]


def test_smoke_two_computation_nodes_one_edge(cs_conn):
    g = read_project_graph(cs_conn, "proj_smoke")
    nodes = {n.id: n for n in g.nodes}
    assert set(nodes) == {"c0", "c1"}
    assert all(n.kind == "computation" for n in g.nodes)

    # c0 produces stats.csv, consumes nothing (a root)
    assert [a.filename for a in nodes["c0"].output_surface] == ["stats.csv"]
    assert nodes["c0"].input_surface == ()
    # c1 consumes stats.csv, produces note.txt
    assert [a.filename for a in nodes["c1"].input_surface] == ["stats.csv"]
    assert [a.filename for a in nodes["c1"].output_surface] == ["note.txt"]
    # provenance carried through for later
    assert nodes["c1"].cs_frame_id == "fd041418" and nodes["c1"].cell_index == 1
    assert nodes["c0"].code and "stats.csv" in nodes["c0"].code
    # cells no longer duplicate the frame's task message — just "cell N"; both sit under one frame
    assert nodes["c0"].label == "cell 0" and nodes["c1"].label == "cell 1"
    assert g.frames == (Frame(id="fd041418", label="Compute Normal Distribution Statistics"),)

    # exactly one edge: c0 -> c1 via stats.csv's version, labeled
    assert len(g.edges) == 1
    (e,) = g.edges
    assert (e.src_node_id, e.dst_node_id) == ("c0", "c1")
    assert e.via_artifact_version_id == "v_stats"
    assert e.reference_name == "stats.csv"


def test_upload_becomes_a_source_node(cs_conn):
    g = read_project_graph(cs_conn, "proj_upload")
    nodes = {n.id: n for n in g.nodes}
    assert set(nodes) == {"source:v_in", "c2"}
    assert nodes["source:v_in"].kind == "source"
    assert [a.filename for a in nodes["source:v_in"].output_surface] == ["input.csv"]
    assert nodes["c2"].kind == "computation"
    assert [a.filename for a in nodes["c2"].input_surface] == ["input.csv"]
    # source keeps its filename, no producing frame; the consumer cell is "cell 0" in frame f2
    assert nodes["source:v_in"].cs_frame_id is None and nodes["c2"].label == "cell 0"
    assert g.frames == (Frame(id="f2", label="Process upload"),)
    (e,) = g.edges
    assert (e.src_node_id, e.dst_node_id) == ("source:v_in", "c2")
    assert e.reference_name == "input.csv"


def test_empty_project_is_empty_graph(cs_conn):
    g = read_project_graph(cs_conn, "nonexistent")
    assert g.nodes == () and g.edges == () and g.frames == ()
