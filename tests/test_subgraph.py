"""Seeded reads: substrate.read_graph builds the upstream cone of its seeds, and the cone stays
verdict-correct — including certain staleness when an artifact's newer head lives OFF the cone.

The off-cone-head test is the regression guard for the derive currency fix: on the old max-in-set
fallback the stale input read as a false CLEAN.
"""

import sqlite3

from provenance_gate.adapters.external import substrate
from provenance_gate.core import audit

_SCHEMA = """
CREATE TABLE projects(id TEXT, name TEXT, updated_at INTEGER);
CREATE TABLE artifacts(id TEXT, project_id TEXT, filename TEXT, latest_version_id TEXT);
CREATE TABLE artifact_versions(
    id TEXT, artifact_id TEXT, version_number INTEGER, checksum TEXT, storage_path TEXT,
    parent_version_id TEXT, producing_cell_id TEXT, frame_id TEXT);
CREATE TABLE artifact_dependencies(
    artifact_version_id TEXT, depends_on_version_id TEXT, reference_name TEXT);
CREATE TABLE execution_log(id TEXT, frame_id TEXT, cell_index INTEGER, source TEXT);
CREATE TABLE frames(id TEXT, task_summary TEXT, name TEXT, parent_frame_id TEXT);
"""


def _divergent_db() -> sqlite3.Connection:
    """x.csv has v1 (used by the report) AND a newer head v2 (a revision on a sibling branch).
    Walking upstream from the report never reaches v2, so v2 is off-cone — the case where the old
    max-in-set fallback would wrongly crown v1 as current."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.execute("INSERT INTO projects VALUES('proj_div','divergent',1)")
    c.executemany("INSERT INTO frames VALUES(?,?,?,?)", [("f", "Analyze", None, None)])
    c.executemany("INSERT INTO execution_log VALUES(?,?,?,?)", [
        ("c0", "f", 0, "write x.csv"),
        ("c_rev", "f", 1, "read x.csv v1; write x.csv v2"),   # the sibling revision (off the cone)
        ("c_report", "f", 2, "read x.csv v1; write report.md"),
    ])
    c.executemany("INSERT INTO artifacts VALUES(?,?,?,?)", [
        ("a_x", "proj_div", "x.csv", "vx2"),        # x.csv's authoritative head is v2
        ("a_r", "proj_div", "report.md", "vr1"),
    ])
    c.executemany("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?)", [
        ("vx1", "a_x", 1, "1", "p/x1", None, "c0", "f"),
        ("vx2", "a_x", 2, "2", "p/x2", "vx1", "c_rev", "f"),
        ("vr1", "a_r", 1, "r", "p/r", None, "c_report", "f"),
    ])
    c.executemany("INSERT INTO artifact_dependencies VALUES(?,?,?)", [
        ("vx2", "vx1", "x.csv"),    # the revision reads v1
        ("vr1", "vx1", "x.csv"),    # the report reads the OLD v1
    ])
    c.commit()
    return c


def test_read_graph_none_is_the_full_project(cs_conn):
    # seeds=None reads the WHOLE project — pin the actual node/edge content so a broken full read
    # can't pass vacuously. (Comparing to read_project_graph would be a tautology: it IS
    # read_graph(seeds=None).) proj_smoke: c0 -> stats.csv -> c1 -> note.txt, under one frame.
    g = substrate.read_graph(cs_conn, "proj_smoke")
    assert {n.id for n in g.nodes} == {"c0", "c1"} and g.cs_project_id == "proj_smoke"
    assert [(e.src_node_id, e.dst_node_id) for e in g.edges] == [("c0", "c1")]
    assert [f.id for f in g.frames] == ["fd041418"]


def test_upstream_cone_scopes_to_ancestors(cs_conn):
    # proj_smoke: c0 -> stats.csv -> c1 -> note.txt. Cone of note.txt=both; of stats.csv=c0 only.
    g_note = substrate.read_graph(cs_conn, "proj_smoke", seeds=["v_note"])
    assert {n.id for n in g_note.nodes} == {"c0", "c1"}
    g_stats = substrate.read_graph(cs_conn, "proj_smoke", seeds=["v_stats"])
    assert {n.id for n in g_stats.nodes} == {"c0"}   # stats.csv has no upstream


def test_foreign_seed_is_dropped():
    # round 3: a seed from ANOTHER project must not start a walk that pulls this project's versions
    # in through it. Seeds are validated to the project first (like an unknown seed -> empty graph).
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.executemany("INSERT INTO projects VALUES(?,?,?)", [("p1", "one", 1), ("p2", "two", 1)])
    c.executemany("INSERT INTO execution_log VALUES(?,?,?,?)",
                  [("c1", None, 0, "p1"), ("c2", None, 0, "p2")])
    c.executemany("INSERT INTO artifacts VALUES(?,?,?,?)",
                  [("a1", "p1", "one.csv", "v1"), ("a2", "p2", "two.csv", "v2")])
    c.executemany("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?)",
                  [("v1", "a1", 1, "1", "p", None, "c1", None),
                   ("v2", "a2", 1, "2", "p", None, "c2", None)])
    c.execute("INSERT INTO artifact_dependencies VALUES('v2','v1','one.csv')")   # p2 seed -> p1 dep
    c.commit()
    assert substrate.read_graph(c, "p1", seeds=["v2"]).nodes == ()   # v2 is not a p1 seed


def test_cone_head_join_ignores_cross_artifact_pointer():
    # an OFF-CONE head pointer to ANOTHER artifact's version (corrupt FK) must not leak that foreign
    # number into currency. The constrained head-join yields NULL off-cone -> derive falls to max.
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.execute("INSERT INTO projects VALUES('p','p',1)")
    c.executemany("INSERT INTO execution_log VALUES(?,?,?,?)",
                  [("c1", None, 0, "x1"), ("c2", None, 1, "x2"), ("co", None, 2, "o")])
    c.executemany("INSERT INTO artifacts VALUES(?,?,?,?)",   # a_x's head is a foreign (a_o) version
                  [("a_x", "p", "x.csv", "v_other"), ("a_o", "p", "o.csv", "vo")])
    c.executemany("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?)",
                  [("vx1", "a_x", 1, "1", "p", None, "c1", None),
                   ("vx2", "a_x", 2, "2", "p", "vx1", "c2", None),
                   ("v_other", "a_o", 99, "9", "p", None, "co", None),
                   ("vo", "a_o", 1, "o", "p", None, "co", None)])
    c.execute("INSERT INTO artifact_dependencies VALUES('vx2','vx1','x.csv')")
    c.commit()
    g = substrate.read_graph(c, "p", seeds=["vx2"])   # cone = a_x's versions; v_other/vo off-cone
    refs = {a.artifact_version_id: a for n in g.nodes for a in n.output_surface}
    assert set(refs) == {"vx1", "vx2"}   # the foreign artifact is not pulled in
    # a_x's cross-artifact head is rejected by the constrained join -> max-in-cone (vx2), not v_o/99
    assert refs["vx2"].is_latest is True and refs["vx2"].latest_version_id == "vx2"
    assert refs["vx2"].latest_version_number == 2


def test_cone_does_not_leak_across_projects():
    # B3: a p1 version whose dependency points at a p2 version (cross-project edge). Seeding that p1
    # version must NOT pull the p2 artifact into a graph labelled p1 (project isolation).
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.executemany("INSERT INTO projects VALUES(?,?,?)", [("p1", "one", 1), ("p2", "two", 1)])
    c.executemany("INSERT INTO execution_log VALUES(?,?,?,?)",
                  [("c1", None, 0, "p1 cell"), ("c2", None, 0, "p2 cell")])
    c.executemany("INSERT INTO artifacts VALUES(?,?,?,?)",
                  [("a1", "p1", "r.csv", "v1"), ("a2", "p2", "x.csv", "v2")])
    c.executemany("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?)",
                  [("v1", "a1", 1, "1", "p", None, "c1", None),
                   ("v2", "a2", 1, "2", "p", None, "c2", None)])
    c.execute("INSERT INTO artifact_dependencies VALUES('v1','v2','x.csv')")   # p1 -> p2 (cross)
    c.commit()
    g = substrate.read_graph(c, "p1", seeds=["v1"])
    files = {a.filename for n in g.nodes for a in list(n.output_surface) + list(n.input_surface)}
    assert g.cs_project_id == "p1" and {n.id for n in g.nodes} == {"c1"}
    assert "x.csv" not in files   # the p2 artifact must not leak into p1's cone


def test_walk_does_not_bridge_through_a_foreign_project():
    # p1 seed -> (dep) p2 -> (dep) a DIFFERENT p1 version. Scoping only the FETCH would still
    # pull that p1_other back in (reachable ONLY through p2). Scoping the WALK stops at the border.
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.executemany("INSERT INTO projects VALUES(?,?,?)", [("p1", "one", 1), ("p2", "two", 1)])
    c.executemany("INSERT INTO execution_log VALUES(?,?,?,?)",
                  [("c1", None, 0, "seed"), ("c2", None, 0, "p2"), ("co", None, 1, "p1 other")])
    c.executemany("INSERT INTO artifacts VALUES(?,?,?,?)",
                  [("a1", "p1", "r.csv", "v1"), ("a2", "p2", "x.csv", "v2"),
                   ("ao", "p1", "other.csv", "vo")])
    c.executemany("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?)",
                  [("v1", "a1", 1, "1", "p", None, "c1", None),
                   ("v2", "a2", 1, "2", "p", None, "c2", None),
                   ("vo", "ao", 1, "o", "p", None, "co", None)])
    c.executemany("INSERT INTO artifact_dependencies VALUES(?,?,?)",
                  [("v1", "v2", "x.csv"), ("v2", "vo", "other.csv")])   # v1 -> v2(p2) -> vo(p1)
    c.commit()
    g = substrate.read_graph(c, "p1", seeds=["v1"])
    assert {n.id for n in g.nodes} == {"c1"}   # walk stops at the p2 border; vo not bridged in


def test_full_graph_sees_the_revision_cone_does_not():
    conn = _divergent_db()
    full = {n.id for n in substrate.read_graph(conn, "proj_div").nodes}
    assert full == {"c0", "c_rev", "c_report"}
    # the report's upstream cone excludes the sibling revision c_rev (and its v2)
    cone = {n.id for n in substrate.read_graph(conn, "proj_div", seeds=["vr1"]).nodes}
    assert cone == {"c0", "c_report"}


def test_offcone_head_makes_stale_input_certain():
    # THE fix: report reads x.csv v1; x.csv's head v2 is off-cone. The cone still flags v1 stale.
    # On the old max-in-set fallback (v1 is the only x.csv in the cone) this read as a false CLEAN.
    conn = _divergent_db()
    g = substrate.read_graph(conn, "proj_div", seeds=["vr1"])
    v = audit.audit_graph(g)
    assert v["c_report"].level == audit.STALE_INPUT
    (issue,) = v["c_report"].stale
    assert issue.artifact == "x.csv" and issue.versions == (1,)
    assert issue.current == 2   # the off-cone head number, resolved by the reader's head-join


def _mix_db() -> sqlite3.Connection:
    """A divergent-branch MERGE (the moat case): x.csv v1 feeds branch A (uses OLD v1); a revision
    makes v2 which feeds branch B (uses NEW v2); the report consumes BOTH A and B. The report's
    upstream cone must therefore contain both x.csv versions -> a VERSION_MIX."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.execute("INSERT INTO projects VALUES('proj_mix','mix',1)")
    c.executemany("INSERT INTO frames VALUES(?,?,?,?)", [("f", "Merge", None, None)])
    c.executemany("INSERT INTO execution_log VALUES(?,?,?,?)", [
        ("c0", "f", 0, "write x.csv"), ("cB", "f", 1, "revise x.csv -> v2"),
        ("cX", "f", 2, "read x.csv v1 -> a.csv"), ("cY", "f", 3, "read x.csv v2 -> b.csv"),
        ("cReport", "f", 4, "read a.csv + b.csv -> report.md"),
    ])
    c.executemany("INSERT INTO artifacts VALUES(?,?,?,?)", [
        ("a_x", "proj_mix", "x.csv", "vx2"), ("a_a", "proj_mix", "a.csv", "va1"),
        ("a_b", "proj_mix", "b.csv", "vb1"), ("a_r", "proj_mix", "report.md", "vr1"),
    ])
    c.executemany("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?)", [
        ("vx1", "a_x", 1, "1", "p", None, "c0", "f"),
        ("vx2", "a_x", 2, "2", "p", "vx1", "cB", "f"),
        ("va1", "a_a", 1, "a", "p", None, "cX", "f"),
        ("vb1", "a_b", 1, "b", "p", None, "cY", "f"),
        ("vr1", "a_r", 1, "r", "p", None, "cReport", "f"),
    ])
    c.executemany("INSERT INTO artifact_dependencies VALUES(?,?,?)", [
        ("vx2", "vx1", "x.csv"), ("va1", "vx1", "x.csv"), ("vb1", "vx2", "x.csv"),
        ("vr1", "va1", "a.csv"), ("vr1", "vb1", "b.csv"),
    ])
    c.commit()
    return c


def test_cone_captures_version_mix():
    # seeding the merging node pulls BOTH divergent branches into the cone -> the moat verdict fires
    conn = _mix_db()
    g = substrate.read_graph(conn, "proj_mix", seeds=["vr1"])
    assert {n.id for n in g.nodes} == {"c0", "cB", "cX", "cY", "cReport"}
    v = audit.audit_graph(g)
    assert v["cReport"].level == audit.VERSION_MIX
    (issue,) = v["cReport"].mixed
    assert issue.artifact == "x.csv" and set(issue.versions) == {1, 2}


def test_read_graph_unknown_seed_is_empty(cs_conn):
    # a seed that resolves to no version must yield an empty graph, not raise
    g = substrate.read_graph(cs_conn, "proj_smoke", seeds=["no_such_version"])
    assert g.nodes == () and g.edges == () and g.frames == ()


def test_read_graph_multi_seed_unions_cones():
    # seeding two leaf outputs unions their upstreams; the shared ancestor c0 appears once
    conn = _mix_db()
    g = substrate.read_graph(conn, "proj_mix", seeds=["va1", "vb1"])
    assert {n.id for n in g.nodes} == {"c0", "cB", "cX", "cY"}   # report NOT seeded -> absent


def test_read_graph_survives_dependency_cycle():
    # a corrupt A<->B dependency cycle: the walk must terminate and derive/audit must not crash
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.execute("INSERT INTO projects VALUES('proj_cyc','cyc',1)")
    c.executemany("INSERT INTO execution_log VALUES(?,?,?,?)",
                  [("cA", None, 0, "read b -> a"), ("cB", None, 1, "read a -> b")])
    c.executemany("INSERT INTO artifacts VALUES(?,?,?,?)",
                  [("a_a", "proj_cyc", "a.csv", "vA"), ("a_b", "proj_cyc", "b.csv", "vB")])
    c.executemany("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?)",
                  [("vA", "a_a", 1, "1", "p", None, "cA", None),
                   ("vB", "a_b", 1, "2", "p", None, "cB", None)])
    c.executemany("INSERT INTO artifact_dependencies VALUES(?,?,?)",
                  [("vA", "vB", "b.csv"), ("vB", "vA", "a.csv")])   # A <-> B cycle
    c.commit()
    g = substrate.read_graph(c, "proj_cyc", seeds=["vA"])   # must terminate
    assert {n.id for n in g.nodes} == {"cA", "cB"}
    assert set(audit.audit_graph(g)) == {"cA", "cB"}        # a verdict per node, no crash
