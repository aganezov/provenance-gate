"""The in-CS HostQueryReader derives the SAME Graph as the external raw-DB reader.

This is the ports payoff made concrete: two readers (raw sqlite vs host.query), one ``core.derive``,
identical graphs — the 'both adapters hand the core identical records' invariant, proven with no CS.
The fake host runs the reader's SQL against the shared in-memory operon fixture (``cs_conn``).
"""

from provenance_gate.adapters.cs_skill.host_query_reader import HostQueryReader
from provenance_gate.adapters.external import substrate


class _FakeHost:
    """Mimics CS ``host.query`` with sqlite: run the SQL, return dict-rows. The reader scopes by
    ``project_id`` in its WHERE, so no implicit project scoping is needed here."""

    def __init__(self, conn):
        self.conn = conn

    def query(self, sql: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql).fetchall()]


def test_host_reader_matches_external_reader(cs_conn):
    for pid in ("proj_smoke", "proj_upload"):
        via_host = HostQueryReader(_FakeHost(cs_conn)).read_project_graph(pid)
        via_db = substrate.read_project_graph(cs_conn, pid)
        # built_at is wall-clock (differs per call) — compare the structural graph, which must match
        assert via_host.cs_project_id == via_db.cs_project_id
        assert via_host.nodes == via_db.nodes
        assert via_host.edges == via_db.edges
        assert via_host.frames == via_db.frames


def test_host_reader_empty_project(cs_conn):
    g = HostQueryReader(_FakeHost(cs_conn)).read_project_graph("nonexistent")
    assert g.nodes == () and g.edges == () and g.frames == ()


def test_reader_excludes_skill_render_outputs(cs_conn):
    # in-CS, the skill's own render outputs must not pollute the DAG
    cs_conn.executemany("INSERT INTO artifacts(id, project_id, filename) VALUES(?,?,?)", [
        ("a_cockpit", "proj_smoke", "cockpit.html"),
        ("a_bundle", "proj_smoke", "cytoscape-dagre.bundle.min.js"),
    ])
    cs_conn.executemany("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?)", [
        ("v_cockpit", "a_cockpit", 1, "c", "p", None, "c0", "fd041418"),
        ("v_bundle", "a_bundle", 1, "c", "p", None, "c0", "fd041418"),
    ])
    cs_conn.commit()
    g = HostQueryReader(_FakeHost(cs_conn), scope_by_host=True).read_project_graph("proj_smoke")
    files = {a.filename for n in g.nodes for a in list(n.output_surface) + list(n.input_surface)}
    assert "cockpit.html" not in files and "cytoscape-dagre.bundle.min.js" not in files
    assert "stats.csv" in files   # real artifacts still present


def test_reader_keeps_null_filename(cs_conn):
    # a NULL filename must NOT be excluded by the self-artifact filter (NULL NOT IN (...) is NULL)
    cs_conn.execute("INSERT INTO artifacts(id, project_id, filename) VALUES(?,?,?)",
                    ("a_nul", "proj_smoke", None))
    cs_conn.execute("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?)",
                    ("v_nul", "a_nul", 1, "c", "p", None, None, None))   # source, NULL filename
    cs_conn.commit()
    g = HostQueryReader(_FakeHost(cs_conn), scope_by_host=True).read_project_graph("proj_smoke")
    vids = {a.artifact_version_id for n in g.nodes for a in n.output_surface}
    assert "v_nul" in vids   # kept despite the NULL filename


def test_scope_by_host_emits_no_project_filter(cs_conn):
    # In CS, host.query(scope="project") isolates the project, so the reader must NOT filter by
    # project_id. Our fake host does not scope, so scope_by_host=True reads BOTH fixture projects —
    # proving the reader deferred scoping to the host (vs the filtered single-project default).
    h = _FakeHost(cs_conn)
    filtered = HostQueryReader(h, scope_by_host=False).read_project_graph("proj_smoke")
    deferred = HostQueryReader(h, scope_by_host=True).read_project_graph("proj_smoke")
    assert len(filtered.nodes) == 2                        # proj_smoke only (project_id filter)
    assert len(deferred.nodes) == len(filtered.nodes) + 2  # + proj_upload's source + cell
