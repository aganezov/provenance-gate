"""The sidecar round-trips a Graph exactly, resync replaces a project's rows, projects isolate."""

from provenance_gate.model import ArtifactRef, Edge, Frame, Graph, Node
from provenance_gate.store import Store


def _ref(**kw) -> ArtifactRef:
    base = dict(
        artifact_version_id="v_stats", artifact_id="a_stats", version_number=1,
        filename="stats.csv", checksum="219df1", storage_path="p/stats.csv",
    )
    base.update(kw)
    return ArtifactRef(**base)


def _graph(built_at: float = 1.0) -> Graph:
    a = _ref()
    n = _ref(artifact_version_id="v_note", artifact_id="a_note", filename="note.txt",
             checksum="299b66", storage_path="p/note.txt")
    c0 = Node(id="c0", cs_project_id="proj", kind="computation", label="make stats.csv",
              output_surface=(a,), cs_frame_id="f", cs_cell_id="c0", cell_index=0, code="write")
    c1 = Node(id="c1", cs_project_id="proj", kind="computation", label="read stats.csv",
              input_surface=(a,), output_surface=(n,), cs_frame_id="f", cs_cell_id="c1",
              cell_index=1, code="read+write")
    e = Edge(id="c0->c1:v_stats", src_node_id="c0", dst_node_id="c1",
             via_artifact_version_id="v_stats", reference_name="stats.csv")
    fr = Frame(id="fr1", label="Compute stats", parent_frame_id=None)
    return Graph(cs_project_id="proj", nodes=(c0, c1), edges=(e,), frames=(fr,), built_at=built_at)


def test_roundtrip_is_exact():
    s = Store()
    g = _graph()
    s.replace_project_graph(g)
    assert s.load_graph("proj") == g  # frozen-dataclass structural equality, order preserved


def test_resync_replaces_a_projects_rows():
    s = Store()
    s.replace_project_graph(_graph(built_at=1.0))
    src = Node(id="source:v_in", cs_project_id="proj", kind="source", label="input.csv",
               output_surface=(_ref(artifact_version_id="v_in", artifact_id="a_in",
                                    filename="input.csv", checksum="aaa", storage_path="p/in"),))
    s.replace_project_graph(Graph(cs_project_id="proj", nodes=(src,), edges=(), built_at=2.0))
    loaded = s.load_graph("proj")
    assert [n.id for n in loaded.nodes] == ["source:v_in"]  # old c0/c1 gone
    assert loaded.edges == () and loaded.built_at == 2.0
    assert loaded.frames == ()  # the old frame was cleared on resync


def test_unknown_project_is_empty():
    g = Store().load_graph("nope")
    assert g.nodes == () and g.edges == () and g.built_at == 0.0


def test_projects_are_isolated():
    s = Store()
    s.replace_project_graph(_graph())
    other = Node(id="x", cs_project_id="proj2", kind="computation", label="x")
    s.replace_project_graph(Graph(cs_project_id="proj2", nodes=(other,), edges=(), built_at=5.0))
    assert {n.id for n in s.load_graph("proj").nodes} == {"c0", "c1"}
    assert {n.id for n in s.load_graph("proj2").nodes} == {"x"}
