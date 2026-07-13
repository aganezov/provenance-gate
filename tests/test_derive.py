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


def test_derive_trusts_offset_authoritative_head():
    # The authoritative head points OUTSIDE the fetched set — the normal subgraph-cone case, where
    # the newer version lives on a branch the cone doesn't include. Trust the pointer: BOTH in-cone
    # versions are correctly non-current (stale). A max(version_number) fallback would falsely crown
    # vx2 as latest — a false CLEAN on staleness — the cone-correctness this guards. The reader's
    # head-join carries the head number even when its row is off-cone.
    versions = _two_versions("vx3_offcone")
    for v in versions.values():
        v["latest_version_number"] = 3
    g = derive.derive_graph("proj_x", versions, [], _TWO_CELLS, [], built_at=1.0)
    refs = {a.artifact_version_id: a for n in g.nodes for a in n.output_surface}
    assert refs["vx1"].is_latest is False and refs["vx2"].is_latest is False   # head is off-cone
    for a in refs.values():
        assert a.latest_version_id == "vx3_offcone" and a.latest_version_number == 3


def test_derive_dangling_head_falls_back_to_max():
    # A head pointer with NO resolvable version (dangling FK: it points to a row absent from the DB,
    # so the reader's head-join yields no number) is UNRESOLVABLE — a trust gate must not turn
    # corrupt metadata into a certain verdict. Fall back to max(version_number) (the safe behavior),
    # not trust the dangling id (which would flag EVERY version stale, current=None). Unlike a VALID
    # off-cone head, which carries a join number and IS trusted (test above).
    g = derive.derive_graph("proj_x", _two_versions("vx_dangling"), [], _TWO_CELLS, [], built_at=1)
    refs = {a.artifact_version_id: a for n in g.nodes for a in n.output_surface}
    assert refs["vx1"].is_latest is False and refs["vx2"].is_latest is True   # max fallback
    for a in refs.values():
        assert a.latest_version_id == "vx2" and a.latest_version_number == 2


def test_derive_trusts_in_set_head_with_null_version_number():
    # a VALID in-set head whose version_number is NULL is still RESOLVABLE (we have its row and it's
    # the same artifact) — trust it, don't fall back to max. Only an UNRESOLVABLE off-set head (no
    # row, no join number) falls back. Guards the B2 gate against over-firing on a NULL number.
    versions = {
        "vh": {"id": "vh", "artifact_id": "a_x", "version_number": None, "checksum": "h",
               "storage_path": "p", "parent_version_id": None, "producing_cell_id": "c1",
               "frame_id": None, "filename": "x.csv", "latest_version_id": "vh"},
        "vo": {"id": "vo", "artifact_id": "a_x", "version_number": 1, "checksum": "o",
               "storage_path": "p", "parent_version_id": None, "producing_cell_id": "c2",
               "frame_id": None, "filename": "x.csv", "latest_version_id": "vh"},
    }
    g = derive.derive_graph("proj_x", versions, [], _TWO_CELLS, [], built_at=1)
    refs = {a.artifact_version_id: a for n in g.nodes for a in n.output_surface}
    assert refs["vh"].is_latest is True and refs["vo"].is_latest is False   # trust the in-set head
    assert all(a.latest_version_id == "vh" for a in refs.values())


def _disagree(order):
    # two versions of a_x that DISAGREE on the head pointer (each names itself) — a corruption/
    # concurrency artifact. Both carry a resolvable join number so the pointer path is taken.
    rows = {
        "vx1": {"id": "vx1", "artifact_id": "a_x", "version_number": 1, "checksum": "1",
                "storage_path": "p", "parent_version_id": None, "producing_cell_id": "c1",
                "frame_id": None, "filename": "x.csv",
                "latest_version_id": "vx1", "latest_version_number": 1},
        "vx2": {"id": "vx2", "artifact_id": "a_x", "version_number": 2, "checksum": "2",
                "storage_path": "p", "parent_version_id": "vx1", "producing_cell_id": "c2",
                "frame_id": None, "filename": "x.csv",
                "latest_version_id": "vx2", "latest_version_number": 2},
    }
    return {vid: rows[vid] for vid in order}


def test_derive_head_selection_deterministic_when_pointers_disagree():
    # disagreeing head pointers must resolve identically regardless of row/scan order (not "first
    # row wins"), so is_latest can't flip across derives of the same state.
    a = derive.derive_graph("proj_x", _disagree(["vx1", "vx2"]), [], _TWO_CELLS, [], built_at=1)
    b = derive.derive_graph("proj_x", _disagree(["vx2", "vx1"]), [], _TWO_CELLS, [], built_at=1)
    la = {r.artifact_version_id: r.is_latest for n in a.nodes for r in n.output_surface}
    lb = {r.artifact_version_id: r.is_latest for n in b.nodes for r in n.output_surface}
    assert la == lb   # deterministic winner regardless of order


def test_derive_ignores_cross_artifact_head():
    # a dangling / wrong-artifact latest_version_id (a CS FK anomaly) must NOT set another
    # artifact's version as this one's head — fall back to max rather than corrupt currency.
    versions = _two_versions("v_other")   # a_x's rows point their head at a DIFFERENT artifact
    versions["v_other"] = {"id": "v_other", "artifact_id": "a_other", "version_number": 9,
                           "checksum": "o", "storage_path": "p/o", "parent_version_id": None,
                           "producing_cell_id": "c3", "frame_id": None, "filename": "o.csv",
                           "latest_version_id": "v_other"}
    cells = dict(_TWO_CELLS, c3={"id": "c3", "frame_id": None, "cell_index": 3, "source": "s3"})
    g = derive.derive_graph("proj_x", versions, [], cells, [], built_at=1.0)
    refs = {a.artifact_version_id: a for n in g.nodes for a in n.output_surface}
    # a_x's cross-artifact head is rejected -> fall back to max (vx2 current), not v_other
    assert refs["vx1"].is_latest is False and refs["vx2"].is_latest is True
    assert refs["vx1"].latest_version_id == "vx2" and refs["vx2"].latest_version_id == "vx2"
    assert refs["v_other"].is_latest is True   # a_other's own head is honored (same artifact)


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



def test_derive_keeps_zero_input_output_in_producing_cell():
    # A cell that writes without reading anything stays in its producing cell; a source is a null
    # producing_cell_id, not an empty input set.
    def v(vid, aid, cell, fn):
        return {"id": vid, "artifact_id": aid, "version_number": 1, "checksum": vid,
                "storage_path": "p/" + vid, "parent_version_id": None,
                "producing_cell_id": cell, "frame_id": "f1" if cell else None, "filename": fn}
    versions = {
        "v_seed": v("v_seed", "a_seed", None, "seed.csv"),     # uploaded, no producing cell
        "v_gen": v("v_gen", "a_gen", "c1", "generated.csv"),   # written by c1 from nothing
        "v_read": v("v_read", "a_read", "c1", "derived.csv"),  # also c1, reads the seed
    }
    deps = [{"consumer_v": "v_read", "input_v": "v_seed", "reference_name": "seed.csv"}]
    cells = {"c1": {"id": "c1", "frame_id": "f1", "cell_index": 0, "source": "gen; read seed"}}
    frames = [{"id": "f1", "task_summary": "t", "name": None, "parent_frame_id": None}]
    g = derive.derive_graph("p", versions, deps, cells, frames, built_at=1.0)

    assert {n.id for n in g.nodes} == {"source:v_seed", "c1"}
    c1 = next(n for n in g.nodes if n.id == "c1")
    assert c1.kind == "computation"
    assert {a.filename for a in c1.output_surface} == {"generated.csv", "derived.csv"}
    assert [a.filename for a in c1.input_surface] == ["seed.csv"]

    from provenance_gate.core.audit import CLEAN, audit_graph
    assert audit_graph(g)[c1.id].level == CLEAN  # zero-input output causes no spurious verdict
