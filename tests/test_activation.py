"""Lazy activation: derive-on-read, throttled cache, change-detection (changed/unchanged)."""

import pytest

from provenance_gate.adapters.external import activation


@pytest.fixture(autouse=True)
def _reset_throttle():
    activation._last_derive.clear()


def test_first_read_derives(cs_db_file, tmp_path):
    state = str(tmp_path / "state")
    g, status = activation.get_fresh(cs_db_file, state, "proj_smoke", min_interval=0)
    assert status == activation.CHANGED  # empty sidecar -> first derive writes
    assert {n.id for n in g.nodes} == {"c0", "c1"}
    assert (tmp_path / "state" / "proj_smoke" / "sidecar.db").exists()


def test_throttle_serves_cache(cs_db_file, tmp_path):
    state = str(tmp_path / "state")
    activation.get_fresh(cs_db_file, state, "proj_smoke", min_interval=0)  # prime the sidecar
    g, status = activation.get_fresh(cs_db_file, state, "proj_smoke", min_interval=999)
    assert status == activation.CACHED  # inside the throttle window -> no CS touch
    assert {n.id for n in g.nodes} == {"c0", "c1"}


def test_unchanged_rederive_reports_unchanged(cs_db_file, tmp_path):
    state = str(tmp_path / "state")
    activation.get_fresh(cs_db_file, state, "proj_smoke", min_interval=0)  # first (changed)
    g, status = activation.get_fresh(cs_db_file, state, "proj_smoke", min_interval=0)  # same data
    assert status == activation.UNCHANGED  # re-derived, identical -> nothing rewritten
    assert {n.id for n in g.nodes} == {"c0", "c1"}


def test_only_read_projects_get_sidecars(cs_db_file, tmp_path):
    state = tmp_path / "state"
    activation.get_fresh(cs_db_file, str(state), "proj_smoke", min_interval=0)
    assert (state / "proj_smoke" / "sidecar.db").exists()
    assert not (state / "proj_upload").exists()  # never read -> never derived (lazy)


def test_frame_only_change_triggers_changed(cs_db_file, tmp_path):
    import sqlite3
    state = str(tmp_path / "state")
    activation.get_fresh(cs_db_file, state, "proj_smoke", min_interval=0)  # first derive (changed)
    c = sqlite3.connect(cs_db_file)  # mutate ONLY the frame's task_summary; nodes/edges unchanged
    c.execute("UPDATE frames SET task_summary='Edited title' WHERE id='fd041418'")
    c.commit()
    c.close()
    g, status = activation.get_fresh(cs_db_file, state, "proj_smoke", min_interval=0)
    assert status == activation.CHANGED  # a frame-label-only change must resync the sidecar
    assert any(f.label == "Edited title" for f in g.frames)


def test_empty_project_gets_a_real_built_at(cs_db_file, tmp_path):
    state = str(tmp_path / "state")
    g, status = activation.get_fresh(cs_db_file, state, "proj_empty", min_interval=0)  # unknown pid
    assert status == activation.CHANGED  # first-ever derive stores even an empty graph
    assert g.nodes == () and g.built_at > 0  # a real built_at, not epoch 0
