"""The in-CS HostQueryReader derives the SAME Graph as the external raw-DB reader.

This is the ports payoff made concrete: two readers (raw sqlite vs host.query), one ``core.derive``,
identical graphs — the 'both adapters hand the core identical records' invariant, proven with no CS.
The fake host runs the reader's SQL against the shared in-memory operon fixture (``cs_conn``).

Parity holds on the science graph; it is exact only for self-artifact-free projects, because the
in-CS reader *deliberately* strips SELF_ARTIFACTS (the skill's own cockpit outputs) while the
external reader — which observes arbitrary projects and has no notion of "self" — does not. The
parity fixtures carry no self-artifacts, so the two agree; test_reader_excludes_skill_render_outputs
pins the in-CS-only exclusion separately.
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


def test_host_reader_matches_external_on_a_cone(cs_conn):
    # the SEEDED cone read must also agree between the adapters — not just the full graph — so the
    # parallel cone SQL (walk expander + head-join) can't drift between them
    for seeds in (["v_note"], ["v_stats"]):
        via_host = HostQueryReader(_FakeHost(cs_conn)).read_graph("proj_smoke", seeds=seeds)
        via_db = substrate.read_graph(cs_conn, "proj_smoke", seeds=seeds)
        assert via_host.nodes == via_db.nodes
        assert via_host.edges == via_db.edges
        assert via_host.frames == via_db.frames


def test_host_reader_matches_external_offcone_currency(cs_conn):
    # give stats.csv a newer head (v2) the note.txt cone won't include (off-cone). Both readers must
    # agree AND flag the in-cone v_stats non-current — proving the in-CS head-join currency.
    cs_conn.execute("UPDATE artifacts SET latest_version_id='v_stats2' WHERE id='a_stats'")
    cs_conn.execute("INSERT INTO execution_log VALUES('c_rev','fd041418',9,'revise stats')")
    cs_conn.execute("INSERT INTO artifact_versions VALUES"
                    "('v_stats2','a_stats',2,'x','p','v_stats','c_rev','fd041418')")
    cs_conn.execute("INSERT INTO artifact_dependencies VALUES('v_stats2','v_stats','stats.csv')")
    cs_conn.commit()
    via_host = HostQueryReader(_FakeHost(cs_conn)).read_graph("proj_smoke", seeds=["v_note"])
    via_db = substrate.read_graph(cs_conn, "proj_smoke", seeds=["v_note"])
    assert via_host.nodes == via_db.nodes and {n.id for n in via_host.nodes} == {"c0", "c1"}
    refs = {a.artifact_version_id: a
            for n in via_host.nodes for a in list(n.output_surface) + list(n.input_surface)}
    assert refs["v_stats"].is_latest is False and refs["v_stats"].latest_version_number == 2


def test_resolve_seeds_by_filename_artifact_version(cs_conn):
    r = HostQueryReader(_FakeHost(cs_conn))
    assert r.resolve_seeds("proj_smoke", "note.txt") == {"v_note"}    # filename -> its version(s)
    assert r.resolve_seeds("proj_smoke", "a_stats") == {"v_stats"}    # artifact id -> version(s)
    assert r.resolve_seeds("proj_smoke", ["v_stats"]) == {"v_stats"}  # version id -> itself
    assert r.resolve_seeds("proj_smoke", "nope") == set()             # unmatched -> empty


def test_resolve_seeds_excludes_self_artifacts(cs_conn):
    # focusing the skill's OWN render output must resolve to nothing (caller gets focus_unresolved),
    # not a seed that read_graph's self-exclusion would then silently drop into an empty cone.
    cs_conn.execute("INSERT INTO artifacts VALUES('a_ck','proj_smoke','cockpit.html','v_ck')")
    cs_conn.execute("INSERT INTO artifact_versions VALUES"
                    "('v_ck','a_ck',1,'c','p',NULL,'c0','fd041418')")
    cs_conn.commit()
    r = HostQueryReader(_FakeHost(cs_conn))
    assert r.resolve_seeds("proj_smoke", "cockpit.html") == set()   # self-artifact -> not a seed
    assert r.resolve_seeds("proj_smoke", "note.txt") == {"v_note"}  # a real file still resolves


def test_host_reader_chunks_a_large_cone(cs_conn):
    # a cone larger than the 900 chunk size must be fetched across MULTIPLE host.query calls and
    # merged (the in-CS analogue of substrate's chunking). A star: one consumer over N sources.
    n = 950
    cs_conn.execute("INSERT INTO projects VALUES('big','big',1)")
    cs_conn.execute("INSERT INTO execution_log VALUES('big_cc',NULL,0,'consumer')")
    cs_conn.execute("INSERT INTO artifacts VALUES('big_a','big','big_out.csv','big_v')")   # fresh
    cs_conn.execute("INSERT INTO artifact_versions VALUES"
                    "('big_v','big_a',1,'c','p',NULL,'big_cc',NULL)")
    for i in range(n):
        cs_conn.execute("INSERT INTO artifacts VALUES(?,?,?,?)",
                        (f"big_a{i}", "big", f"big_s{i}.csv", f"big_v{i}"))
        cs_conn.execute("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?)",
                        (f"big_v{i}", f"big_a{i}", 1, "c", "p", None, None, None))   # source
        cs_conn.execute("INSERT INTO artifact_dependencies VALUES('big_v',?,?)",
                        (f"big_v{i}", f"big_s{i}.csv"))
    cs_conn.commit()
    g = HostQueryReader(_FakeHost(cs_conn)).read_graph("big", seeds=["big_v"])
    assert sum(1 for node in g.nodes if node.kind == "source") == n   # all N merged across chunks


def test_host_reader_drops_foreign_seed(cs_conn):
    # parity with substrate: a foreign-project seed must not pull this project's versions in. Add a
    # cross-project dep (proj_upload's v_out -> proj_smoke's v_stats), then seed proj_smoke w/ it.
    cs_conn.execute("INSERT INTO artifact_dependencies VALUES('v_out','v_stats','stats.csv')")
    cs_conn.commit()
    g = HostQueryReader(_FakeHost(cs_conn)).read_graph("proj_smoke", seeds=["v_out"])
    assert g.nodes == ()   # v_out belongs to proj_upload -> dropped, no leak into proj_smoke


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
