"""The bundled skill kernel executes end-to-end: a fake ``host`` -> inlined HostQueryReader ->
inlined core.derive -> inlined core.audit. This proves the inline bundle + the kernel wiring (the
audit logic itself is covered by test_audit.py; the reader parity by test_cs_skill_reader.py).

Regression guard for the build: if inlining drops an import, mis-orders a module, or the kernel's
host adapter drifts, exec'ing the generated source here fails.
"""

import importlib.util
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("build_skill", _ROOT / "design" / "build_skill.py")
build_skill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_skill)


class _FakeSkills:
    """host.skills stub for render_cockpit: the .html reference carries the placeholder; the bundle
    is opaque."""

    def read(self, name, path):
        # stub of the generated full-cockpit template: the bundle marker + the two baked-data slots
        html = ('<html><head><script src="__BUNDLE_SRC__"></script></head>'
                '<body><script>window.P=__PROJECT__;window.G=__GRAPH__;</script></body></html>')
        return {"content": html if path.endswith(".html") else "/*VENDOR*/"}


class _FakeHost:
    """Stands in for CS's injected ``host``: run the SQL, hand back rows (no project scoping, so the
    kernel's scope_by_host=True reads the whole fixture — both projects)."""

    def __init__(self, conn, bundle_saved=True):
        self.conn = conn
        self.skills = _FakeSkills()
        self._bundle_saved = bundle_saved

    def query(self, sql, scope=None):
        if "a.filename = 'cytoscape-dagre.bundle.min.js'" in sql:  # bundle-id lookup
            return [{"id": "BUNDLE_VID"}] if self._bundle_saved else []
        return self.conn.execute(sql).fetchall()

    def artifact_marker(self, vid):
        return "{{artifact:" + vid + "}}"


def _exec_kernel(fake_host):
    src = build_skill._kernel_source()
    ns = {"host": fake_host}  # render writes files; the agent saves them, not the kernel
    exec(compile(src, "provenance_gate_kernel", "exec"), ns)  # noqa: S102 (our build output)
    return ns


def test_generated_kernel_compiles():
    # compiling the inlined bundle must not raise (catches import/order regressions)
    src = build_skill._kernel_source()
    compile(src, "provenance_gate_kernel", "exec")


def test_inlined_kernel_audits_project(cs_conn):
    ns = _exec_kernel(_FakeHost(cs_conn))
    out = ns["audit_project"]()
    assert set(out) == {"project", "cells", "clean", "flagged"}
    # fake host doesn't scope, so this reads BOTH fixture projects: 4 nodes, all clean
    assert out["cells"] == 4 and out["clean"] == 4 and out["flagged"] == []


def test_inlined_kernel_audits_input_lineage(cs_conn):
    ns = _exec_kernel(_FakeHost(cs_conn))
    clean = ns["audit_input_lineage"](["stats.csv"], planned_output="figure.png")
    assert clean["verdict"] == "clean" and clean["missing_inputs"] == []
    missing = ns["audit_input_lineage"](["nope.csv"])
    assert missing["verdict"] == "LINEAGE_MISSING" and missing["missing_inputs"] == ["nope.csv"]


def test_inlined_kernel_renders_cockpit(cs_conn, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # render_cockpit writes cockpit.html + the bundle into CWD
    ns = _exec_kernel(_FakeHost(cs_conn))
    out = ns["render_cockpit"]()
    assert out["nodes"] == 4 and out["files"] == ["cockpit.html"]
    html = (tmp_path / "cockpit.html").read_text()
    assert "{{artifact:BUNDLE_VID}}" in html      # bundle referenced by artifact marker
    assert '"cs_project_id"' in html               # asdict(graph) baked as the /api/graph shape
    assert '"verdict"' in html                     # each node carries its computed verdict
    assert "cell 0" in html                        # a node label present in the baked graph
    assert "__BUNDLE_SRC__" not in html
    assert "__GRAPH__" not in html and "__PROJECT__" not in html


def test_cockpit_escapes_every_lt_in_baked_json(cs_conn, tmp_path, monkeypatch):
    # EVERY '<' in a baked value must become < so no raw '<' can form an HTML tag inside the inline
    # <script> holding GRAPH: covers </script> AND the <!--<script> double-escape a '</'-only seal
    # misses (the regression both reviews caught). Add a source artifact with such a name.
    cs_conn.execute("INSERT INTO artifacts(id, project_id, filename) VALUES(?,?,?)",
                    ("a_x", "proj_smoke", "<!--<script>evil.csv"))
    cs_conn.execute("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?)",
                    ("v_x", "a_x", 1, "c", "p", None, None, None))   # a source (no producing cell)
    cs_conn.commit()
    monkeypatch.chdir(tmp_path)
    ns = _exec_kernel(_FakeHost(cs_conn))
    ns["render_cockpit"]()
    html = (tmp_path / "cockpit.html").read_text()
    assert "<!--<script>evil" not in html and "<script>evil" not in html   # no raw '<' survives
    assert "\\u003c!--\\u003cscript>evil.csv" in html   # sealed form, name intact


def test_generated_full_template_structure():
    # Exercise the REAL _skill_cockpit_html (not the stub): vendor stripped to the __BUNDLE_SRC__
    # marker, PG:SKILL-DATA override + fallback injected in order, snapshot on, lean placeholders
    # gone. Backstops the transform the render tests stub out.
    html = build_skill._skill_cockpit_html()
    assert "PG:VENDOR" not in html                                   # inlined vendor stripped
    assert html.count('<script src="__BUNDLE_SRC__">') == 1          # bundle marker inserted once
    assert html.count("__GRAPH__") == 1 and html.count("__PROJECT__") == 1
    assert "__ELEMENTS__" not in html and "__NODELIST__" not in html  # lean placeholders gone
    assert "PG:SKILL-DATA" in html and "pg-skill-fallback" in html
    assert "window.__PG_SNAPSHOT" in html                            # snapshot mode activated in-CS
    # injection ORDER: bundle marker in <head>, data override after PG:LOG, fallback before </body>
    i_marker, i_data, i_fb = (html.index("__BUNDLE_SRC__"), html.index("PG:SKILL-DATA"),
                              html.index("pg-skill-fallback"))
    assert i_marker < i_data < i_fb


def test_render_outputs_are_all_excluded(cs_conn, tmp_path, monkeypatch):
    # drift guard: every file render_cockpit save_artifacts must be one the reader self-excludes
    from provenance_gate.adapters.cs_skill.host_query_reader import SELF_ARTIFACTS
    monkeypatch.chdir(tmp_path)
    first = _exec_kernel(_FakeHost(cs_conn, bundle_saved=False))["render_cockpit"]()  # bundle
    second = _exec_kernel(_FakeHost(cs_conn, bundle_saved=True))["render_cockpit"]()  # cockpit.html
    assert set(first["files"]) | set(second["files"]) == set(SELF_ARTIFACTS)
