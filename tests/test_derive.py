"""core.derive is reader-agnostic: fed plain dict records (no sqlite, no CS), it produces the Graph.

This is the whole point of the ports split — the external raw-DB reader and the future in-CS
host.query reader hand these *same* records to one derive, so both surfaces derive identically.
"""

from provenance_gate.core import derive


def _records():
    versions = {
        "v_in": {  # an upload with no producing cell -> a source node
            "id": "v_in", "artifact_id": "a_in", "version_number": 1, "checksum": "aaa",
            "storage_path": "p/in.csv", "parent_version_id": None,
            "producing_cell_id": None, "frame_id": None, "filename": "in.csv",
        },
        "v_out": {  # produced by cell c1 in frame f1
            "id": "v_out", "artifact_id": "a_out", "version_number": 1, "checksum": "bbb",
            "storage_path": "p/out.csv", "parent_version_id": None,
            "producing_cell_id": "c1", "frame_id": "f1", "filename": "out.csv",
        },
    }
    deps = [{"consumer_v": "v_out", "input_v": "v_in", "reference_name": "in.csv"}]
    cells = {"c1": {"id": "c1", "frame_id": "f1", "cell_index": 3, "source": "read in.csv"}}
    frames = [{"id": "f1", "task_summary": "Do the thing", "name": None, "parent_frame_id": None}]
    return versions, deps, cells, frames


def test_derive_builds_graph_from_plain_dicts():
    g = derive.derive_graph("proj_x", *_records(), built_at=123.0)
    assert g.cs_project_id == "proj_x" and g.built_at == 123.0
    assert {n.id for n in g.nodes} == {"source:v_in", "c1"}

    comp = next(n for n in g.nodes if n.id == "c1")
    assert comp.kind == "computation" and comp.label == "cell 3"
    assert [a.filename for a in comp.input_surface] == ["in.csv"]
    assert [a.filename for a in comp.output_surface] == ["out.csv"]
    assert comp.code == "read in.csv"

    src = next(n for n in g.nodes if n.id == "source:v_in")
    assert src.kind == "source" and src.label == "in.csv"

    assert [(e.src_node_id, e.dst_node_id) for e in g.edges] == [("source:v_in", "c1")]
    assert [f.label for f in g.frames] == ["Do the thing"]


def test_derive_drops_unreferenced_frames():
    # an over-fetched frame nobody's node references must not leak into the graph
    versions, deps, cells, frames = _records()
    frames.append({"id": "f_orphan", "task_summary": "x", "name": None, "parent_frame_id": None})
    g = derive.derive_graph("proj_x", versions, deps, cells, frames, built_at=1.0)
    assert {f.id for f in g.frames} == {"f1"}


def test_derive_empty_when_no_versions():
    g = derive.derive_graph("proj_empty", {}, [], {}, [], built_at=5.0)
    assert g.nodes == () and g.edges == () and g.frames == () and g.built_at == 5.0
