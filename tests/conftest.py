"""Shared test fixture: a tiny in-memory operon-shaped CS DB (only the columns we read).

Holds two projects:
  - proj_smoke : the real drive-smoke-test shape — two computation cells, one consuming the
                 other's artifact (stats.csv -> note.txt).
  - proj_upload: an upload (no producing cell) consumed by a cell -> exercises source nodes.
"""

import sqlite3

import pytest

SCHEMA = """
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


@pytest.fixture
def cs_conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    c.executemany("INSERT INTO projects VALUES(?,?,?)", [
        ("proj_smoke", "drive-smoke-test", 200),
        ("proj_upload", "upload-demo", 100),
    ])
    # root_frame_id = the conversation (a top-level chat's frame is its own root); project_id is
    # its project — both real operon columns the chat-scoped review resolves the current chat by.
    c.executemany("INSERT INTO frames VALUES(?,?,?,?,?,?)", [
        ("fd041418", "Compute Normal Distribution Statistics", "Normal stats", None,
         "fd041418", "proj_smoke"),
        ("f2", "Process upload", "Upload", None, "f2", "proj_upload"),
        ("f_empty", "Idle chat", "Idle", None, "f_empty", "proj_smoke"),  # a chat that made nothing
        # an empty chat in proj_upload (NOT the recency winner) — pins that the current project is
        # resolved from the FRAME, not the recency heuristic (which would pick proj_smoke).
        ("f_empty_up", "Idle upload chat", "Idle up", None, "f_empty_up", "proj_upload"),
    ])
    c.executemany("INSERT INTO execution_log VALUES(?,?,?,?)", [
        ("c0", "fd041418", 0, "np.random.seed(42); ... write stats.csv"),
        ("c1", "fd041418", 1, "read stats.csv; write note.txt"),
        ("c2", "f2", 0, "read input.csv; write out.csv"),
    ])
    # latest_version_id = the artifact's authoritative head (here each artifact has one version)
    c.executemany("INSERT INTO artifacts VALUES(?,?,?,?)", [
        ("a_stats", "proj_smoke", "stats.csv", "v_stats"),
        ("a_note", "proj_smoke", "note.txt", "v_note"),
        ("a_in", "proj_upload", "input.csv", "v_in"),
        ("a_out", "proj_upload", "out.csv", "v_out"),
    ])
    c.executemany("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?)", [
        ("v_stats", "a_stats", 1, "219df1", "proj_smoke/a_stats/stats.csv", None, "c0", "fd041418"),
        ("v_note", "a_note", 1, "299b66", "proj_smoke/a_note/note.txt", None, "c1", "fd041418"),
        ("v_in", "a_in", 1, "aaa", "proj_upload/a_in/input.csv", None, None, None),  # upload
        ("v_out", "a_out", 1, "bbb", "proj_upload/a_out/out.csv", None, "c2", "f2"),
    ])
    c.executemany("INSERT INTO artifact_dependencies VALUES(?,?,?)", [
        ("v_note", "v_stats", "stats.csv"),
        ("v_out", "v_in", "input.csv"),
    ])
    c.commit()
    return c


@pytest.fixture
def cs_db_file(cs_conn, tmp_path):
    """The in-memory CS fixture materialized to a file — for code paths that open a DB path."""
    path = tmp_path / "cs.db"
    dst = sqlite3.connect(path)
    with dst:
        cs_conn.backup(dst)
    dst.close()
    return str(path)
