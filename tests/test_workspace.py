"""State-dir workspace: per-project layout on disk, and pid validation blocks path traversal."""

import os

import pytest

from provenance_gate import substrate, workspace
from provenance_gate.store import Store


def test_pid_validation():
    assert workspace.is_valid_pid("proj_cd03c711d180")
    for bad in ["", "..", ".", "a/b", "../x", "a.b", "x/../y", "a b", "abc\n", "abc\t"]:
        assert not workspace.is_valid_pid(bad), bad


def test_unsafe_pid_raises(tmp_path):
    with pytest.raises(ValueError):
        workspace.sidecar_path(tmp_path, "../escape")


def test_sidecar_path_is_per_project(tmp_path):
    p = workspace.sidecar_path(tmp_path, "proj_x", create=True)
    assert p.endswith(os.path.join("proj_x", "sidecar.db"))
    assert (tmp_path / "proj_x").is_dir()


def test_default_state_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PG_STATE_DIR", str(tmp_path / "custom"))
    assert workspace.default_state_dir() == tmp_path / "custom"
    monkeypatch.delenv("PG_STATE_DIR", raising=False)
    assert str(workspace.default_state_dir()) == ".pg"


def test_per_project_sidecars_isolate_on_disk(cs_conn, tmp_path):
    for pid in ("proj_smoke", "proj_upload"):  # mimic per-project derivation
        s = Store(workspace.sidecar_path(tmp_path, pid, create=True))
        s.replace_project_graph(substrate.read_project_graph(cs_conn, pid))
        s.close()
    assert (tmp_path / "proj_smoke" / "sidecar.db").exists()
    assert (tmp_path / "proj_upload" / "sidecar.db").exists()
    s = Store(workspace.sidecar_path(tmp_path, "proj_smoke"))
    try:
        assert {n.id for n in s.load_graph("proj_smoke").nodes} == {"c0", "c1"}
        assert s.load_graph("proj_upload").nodes == ()  # isolated
    finally:
        s.close()
