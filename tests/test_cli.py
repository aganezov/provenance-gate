"""pg-audit CLI: the one-shot audit reports the same flagged cells the gate computes."""

import sqlite3

from provenance_gate.adapters.external.cli import audit_db

# the operon columns the reader/audit touch — a captured project.db has exactly this shape.
_SCHEMA = """
CREATE TABLE projects(id TEXT, name TEXT, updated_at INTEGER);
CREATE TABLE artifacts(id TEXT, project_id TEXT, filename TEXT, latest_version_id TEXT);
CREATE TABLE artifact_versions(
    id TEXT, artifact_id TEXT, version_number INTEGER, checksum TEXT, storage_path TEXT,
    parent_version_id TEXT, producing_cell_id TEXT, frame_id TEXT);
CREATE TABLE artifact_dependencies(
    artifact_version_id TEXT, depends_on_version_id TEXT, reference_name TEXT);
CREATE TABLE execution_log(id TEXT, frame_id TEXT, cell_index INTEGER, source TEXT);
CREATE TABLE frames(
    id TEXT, task_summary TEXT, name TEXT, parent_frame_id TEXT,
    root_frame_id TEXT, project_id TEXT);
"""


def _stale_lineage_db(path: str) -> None:
    # config.csv revised to v2, but report.csv still consumes v1 -> report is stale_input.
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO projects VALUES('p', 'demo', 1)")
    conn.execute("INSERT INTO frames VALUES('f', 'task', 'n', NULL, 'f', 'p')")
    conn.executemany("INSERT INTO execution_log VALUES(?,?,?,?)", [
        ("c_cfg", "f", 0, "write config.csv"),
        ("c_cfg2", "f", 1, "revise config.csv"),
        ("c_use", "f", 2, "read config.csv v1; write report.csv"),
    ])
    conn.execute("INSERT INTO artifacts VALUES('a_cfg', 'p', 'config.csv', 'v_cfg2')")
    conn.execute("INSERT INTO artifacts VALUES('a_rep', 'p', 'report.csv', 'v_rep')")
    conn.executemany("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?)", [
        ("v_cfg1", "a_cfg", 1, "x", "p1", None, "c_cfg", "f"),
        ("v_cfg2", "a_cfg", 2, "y", "p2", "v_cfg1", "c_cfg2", "f"),
        ("v_rep", "a_rep", 1, "z", "p3", None, "c_use", "f"),
    ])
    conn.execute("INSERT INTO artifact_dependencies VALUES('v_rep', 'v_cfg1', 'config.csv')")
    conn.commit()
    conn.close()


def test_pg_audit_flags_stale_lineage(tmp_path):
    path = str(tmp_path / "demo.db")
    _stale_lineage_db(path)
    report = audit_db(path)
    assert report["project"] == "p"
    assert report["cells"] == 3
    assert len(report["flagged"]) == 1
    flag = report["flagged"][0]
    assert flag["verdict"] == "stale_input"
    assert flag["cell"] == "report.csv"
    assert flag["stale"][0]["artifact"] == "config.csv"
    assert flag["stale"][0]["versions"] == [1]
    assert flag["stale"][0]["current"] == 2


def test_pg_audit_clean_project_has_no_flags(cs_db_file):
    report = audit_db(cs_db_file, project_id="proj_smoke")
    assert report["cells"] == 2
    assert report["flagged"] == []
