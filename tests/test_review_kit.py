"""core.review_kit.review_kit: a deterministic evidence brief over a lineage subgraph.

The main fixture mirrors the qc-test-merge shape: a raw source feeds two cells that write v1 and v2
of one artifact (head = v2), and a third cell merges BOTH versions — the version_mix the gate
catches and the LLM misses. review_kit is pure over the Graph, so we build graphs via derive_graph.
Extra fixtures pin the two review-caught bugs (version-granular focus, NULL-filename sort safety),
empty graphs, and that the ordering is real rather than an artifact of node-id order.
"""

from provenance_gate.core import derive
from provenance_gate.core.audit import VERSION_MIX
from provenance_gate.core.review_kit import GO_DEEPER, REVIEW_QUESTION, review_kit


def _v(vid, aid, num, cell, fn, head=None, headnum=None, cs="cs"):
    return {"id": vid, "artifact_id": aid, "version_number": num, "checksum": cs,
            "storage_path": "p/" + vid, "parent_version_id": None,
            "producing_cell_id": cell, "frame_id": "f1" if cell else None, "filename": fn,
            "latest_version_id": head or vid,
            "latest_version_number": headnum if headnum is not None else num}


def _cell(cid, idx, src="code"):
    return {"id": cid, "frame_id": "f1", "cell_index": idx, "source": src}


_FRAME = [{"id": "f1", "task_summary": "t", "name": None, "parent_frame_id": None}]


def _mix_records():
    versions = {
        "raw": _v("raw", "a_raw", 1, None, "raw_cells.csv", cs="rawsum00"),
        "qc1": _v("qc1", "a_qc", 1, "c1", "cells.qc.csv", "qc2", 2, "qcsum1aa"),
        "qc2": _v("qc2", "a_qc", 2, "c2", "cells.qc.csv", "qc2", 2, "qcsum2bb"),
        "rep": _v("rep", "a_rep", 1, "c3", "combined_report.csv", cs="repsum00"),
    }
    deps = [
        {"consumer_v": "qc1", "input_v": "raw", "reference_name": "raw_cells.csv"},
        {"consumer_v": "qc2", "input_v": "raw", "reference_name": "raw_cells.csv"},
        {"consumer_v": "rep", "input_v": "qc1", "reference_name": "cells.qc.csv"},
        {"consumer_v": "rep", "input_v": "qc2", "reference_name": "cells.qc.csv"},
    ]
    cells = {"c1": _cell("c1", 1), "c2": _cell("c2", 2), "c3": _cell("c3", 3)}
    return versions, deps, cells, _FRAME


def _review():
    return review_kit(derive.derive_graph("proj_x", *_mix_records(), built_at=1.0))


def test_scope_and_focus():
    kit = _review()
    assert kit["scope"] == "upstream"
    assert kit["nodes"] == 4  # raw source + c1 + c2 + c3
    assert kit["focus"] == ["combined_report.csv"]  # the sole terminal
    assert kit["question"] == REVIEW_QUESTION and kit["next"] == GO_DEEPER


def test_flags_carry_the_version_mix():
    mix = next(f for f in _review()["flags"] if f["cell"] == "cell 3")
    assert mix["verdict"] == VERSION_MIX
    mixed = next(m for m in mix["mixed"] if m["artifact"] == "cells.qc.csv")
    assert mixed["versions"] == [1, 2] and mixed["current"] == 2


def test_flags_hold_only_the_flagged_cell():
    # the clean producers (cell 1, cell 2) must NOT appear — a spurious flag would be caught here
    flags = _review()["flags"]
    assert len(flags) == 1
    assert {f["cell"] for f in flags} == {"cell 3"}


def test_inventory_marks_current_vs_stale():
    inv = {(a["filename"], a["version"]): a for a in _review()["artifacts"]}
    assert inv[("cells.qc.csv", 1)]["is_latest"] is False
    assert inv[("cells.qc.csv", 2)]["is_latest"] is True
    assert inv[("combined_report.csv", 1)]["is_latest"] is True
    assert inv[("cells.qc.csv", 1)]["checksum"] == "qcsum1aa"


def test_lineage_is_sorted_and_resolves():
    # assert the FULL emitted order (no re-sort in the test) so a dropped/changed lineage sort fails
    got = [(e["from"], e["to"], e["artifact"], e["version"]) for e in _review()["lineage"]]
    assert got == [
        ("raw_cells.csv", "cell 1", "raw_cells.csv", 1),
        ("raw_cells.csv", "cell 2", "raw_cells.csv", 1),
        ("cell 1", "cell 3", "cells.qc.csv", 1),
        ("cell 2", "cell 3", "cells.qc.csv", 2),
    ]


def test_boundary_is_the_raw_source():
    assert _review()["boundary"] == [
        {"artifact": "raw_cells.csv", "version": 1, "checksum": "rawsum00"}
    ]


def test_cells_list_the_computations_for_code_fetch():
    assert _review()["cells"] == ["c1", "c2", "c3"]


# ---- regressions for the two review-caught bugs ----

def _mixed_output_records():
    # c1 writes kept.csv (consumed by c2) AND extra.csv (an unconsumed terminal); c2 -> report.csv
    versions = {
        "kept": _v("kept", "a_kept", 1, "c1", "kept.csv"),
        "extra": _v("extra", "a_extra", 1, "c1", "extra.csv"),
        "report": _v("report", "a_rep", 1, "c2", "report.csv"),
    }
    deps = [{"consumer_v": "report", "input_v": "kept", "reference_name": "kept.csv"}]
    return versions, deps, {"c1": _cell("c1", 1), "c2": _cell("c2", 2)}, _FRAME


def test_focus_is_version_granular_not_node_granular():
    # node-granular focus would drop extra.csv (c1 is a producer of the consumed kept.csv);
    # version-granular keeps it. kept.csv is consumed, so it is NOT a terminal. (bug B regression)
    g = derive.derive_graph("p", *_mixed_output_records(), built_at=1.0)
    assert review_kit(g)["focus"] == ["extra.csv", "report.csv"]


def _null_filename_records():
    versions = {
        "real": _v("real", "a_real", 1, "c1", "real.csv"),
        "nameless": _v("nameless", "a_none", 1, "c1", None),  # CS keeps NULL-filename versions
    }
    return versions, [], {"c1": _cell("c1", 1)}, _FRAME


def test_null_filename_does_not_crash_the_sort():
    # sorted([None, "real.csv"]) would raise TypeError; review_kit must coerce None -> "". (bug A)
    g = derive.derive_graph("p", *_null_filename_records(), built_at=1.0)
    kit = review_kit(g)  # must not raise
    assert kit["focus"] == ["", "real.csv"]
    assert any(a["filename"] == "" for a in kit["artifacts"])


def test_empty_graph_is_a_valid_empty_kit():
    kit = review_kit(derive.empty_graph("p", built_at=1.0))
    assert kit["nodes"] == 0
    assert kit["focus"] == [] and kit["flags"] == [] and kit["lineage"] == []
    assert kit["artifacts"] == [] and kit["boundary"] == [] and kit["cells"] == []


def _two_terminal_records():
    # node ids sort cA < cB, but filenames sort z_last AFTER a_first — so a dropped focus sort would
    # surface them in set/hash order, not alphabetical.
    versions = {
        "za": _v("za", "a_z", 1, "cA", "z_last.csv"),
        "af": _v("af", "a_a", 1, "cB", "a_first.csv"),
    }
    return versions, [], {"cA": _cell("cA", 1), "cB": _cell("cB", 2)}, _FRAME


def test_focus_is_sorted_by_filename_not_node_order():
    g = derive.derive_graph("p", *_two_terminal_records(), built_at=1.0)
    assert review_kit(g)["focus"] == ["a_first.csv", "z_last.csv"]
