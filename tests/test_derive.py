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


def test_derive_flags_latest_version():
    # two versions of the same artifact (a_x), NO latest_version_id on the records -> currency
    # falls back to max(version_number): v1 is stale, v2 is current.
    versions = {
        "vx1": {"id": "vx1", "artifact_id": "a_x", "version_number": 1, "checksum": "1",
                "storage_path": "p/x1", "parent_version_id": None,
                "producing_cell_id": "c1", "frame_id": None, "filename": "x.csv"},
        "vx2": {"id": "vx2", "artifact_id": "a_x", "version_number": 2, "checksum": "2",
                "storage_path": "p/x2", "parent_version_id": "vx1",
                "producing_cell_id": "c2", "frame_id": None, "filename": "x.csv"},
    }
    cells = {"c1": {"id": "c1", "frame_id": None, "cell_index": 1, "source": "s1"},
             "c2": {"id": "c2", "frame_id": None, "cell_index": 2, "source": "s2"}}
    g = derive.derive_graph("proj_x", versions, [], cells, [], built_at=1.0)
    refs = {a.artifact_version_id: a for n in g.nodes for a in n.output_surface}
    assert refs["vx1"].is_latest is False and refs["vx2"].is_latest is True  # max version_number
    # every ref points at the artifact's current version (vx2, v2) — the UI's "(current vN)" chip
    for a in refs.values():
        assert a.latest_version_id == "vx2" and a.latest_version_number == 2


def _two_versions(head_id):
    # a_x with v1 (num 1) and v2 (num 2); both rows carry the artifact's head id (as CS's join does)
    return {
        "vx1": {"id": "vx1", "artifact_id": "a_x", "version_number": 1, "checksum": "1",
                "storage_path": "p/x1", "parent_version_id": None, "producing_cell_id": "c1",
                "frame_id": None, "filename": "x.csv", "latest_version_id": head_id},
        "vx2": {"id": "vx2", "artifact_id": "a_x", "version_number": 2, "checksum": "2",
                "storage_path": "p/x2", "parent_version_id": "vx1", "producing_cell_id": "c2",
                "frame_id": None, "filename": "x.csv", "latest_version_id": head_id},
    }


_TWO_CELLS = {"c1": {"id": "c1", "frame_id": None, "cell_index": 1, "source": "s1"},
              "c2": {"id": "c2", "frame_id": None, "cell_index": 2, "source": "s2"}}


def test_derive_prefers_authoritative_latest_over_max():
    # CS's authoritative head (artifacts.latest_version_id) can point at a NON-highest-numbered
    # version — a rollback/repoint. Currency must follow that head, not max(version_number): here
    # the head is vx1 (older), so the higher-numbered vx2 is NOT current (would be STALE_INPUT).
    g = derive.derive_graph("proj_x", _two_versions("vx1"), [], _TWO_CELLS, [], built_at=1.0)
    refs = {a.artifact_version_id: a for n in g.nodes for a in n.output_surface}
    assert refs["vx1"].is_latest is True and refs["vx2"].is_latest is False   # authoritative wins
    for a in refs.values():
        assert a.latest_version_id == "vx1" and a.latest_version_number == 1


def test_derive_falls_back_to_max_when_authoritative_head_unresolvable():
    # latest_version_id points outside the fetched set (an older/filtered head we didn't load) —
    # fall back to max(version_number) rather than dropping currency for the artifact entirely.
    g = derive.derive_graph("proj_x", _two_versions("vx_not_fetched"), [], _TWO_CELLS, [],
                            built_at=1.0)
    refs = {a.artifact_version_id: a for n in g.nodes for a in n.output_surface}
    assert refs["vx1"].is_latest is False and refs["vx2"].is_latest is True   # max fallback
    for a in refs.values():
        assert a.latest_version_id == "vx2" and a.latest_version_number == 2


def test_derive_latest_tiebreak_is_deterministic():
    # two versions of one artifact tied on version_number: the higher version id wins, stably,
    # regardless of scan/insertion order — so is_latest can't flip across derives (determinism).
    def mk(vid):
        return {"id": vid, "artifact_id": "a_t", "version_number": 1, "checksum": "c",
                "storage_path": "p", "parent_version_id": None,
                "producing_cell_id": "cell_" + vid, "frame_id": None, "filename": "t.csv"}

    cells = {c: {"id": c, "frame_id": None, "cell_index": 0, "source": "s"}
             for c in ("cell_vA", "cell_vB")}
    for order in (["vA", "vB"], ["vB", "vA"]):  # both insertion orders → same winner
        versions = {vid: mk(vid) for vid in order}
        g = derive.derive_graph("proj_t", versions, [], cells, [], built_at=1.0)
        latest = {a.artifact_version_id: a.is_latest for n in g.nodes for a in n.output_surface}
        assert latest == {"vB": True, "vA": False}  # higher id wins the tie, both orders

