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
    """host.skills stub for render_cockpit: the .html reference carries the marker placeholders; the
    saved-once assets (bundle, app JS) are opaque."""

    def read(self, name, path):
        if path.endswith(".html"):  # stub shell: bundle + app-js markers + the two baked-data slots
            return {"content": '<html><head><script src="__BUNDLE_SRC__"></script></head><body>'
                    '<script>window.P=__PROJECT__;window.G=__GRAPH__;</script>'
                    '<script src="__APP_JS_SRC__"></script></body></html>'}
        return {"content": "/*asset " + path + "*/"}   # bundle or cockpit-app.js


class _FakeHost:
    """Stands in for CS's injected ``host``: run the SQL, hand back rows (no project scoping, so the
    kernel's scope_by_host=True reads the whole fixture — both projects)."""

    def __init__(self, conn, assets_saved=True):
        self.conn = conn
        self.skills = _FakeSkills()
        self._assets_saved = assets_saved

    def query(self, sql, scope=None):
        # asset-existence lookups (bundle + app js): saved -> a fake version id, else empty
        if "a.filename = 'cytoscape-dagre.bundle.min.js'" in sql:
            return [{"id": "BUNDLE_VID"}] if self._assets_saved else []
        if "a.filename = 'cockpit-app.js'" in sql:
            return [{"id": "APPJS_VID"}] if self._assets_saved else []
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
    assert "{{artifact:APPJS_VID}}" in html        # app JS referenced by artifact marker
    assert '"cs_project_id"' in html               # asdict(graph) baked as the /api/graph shape
    assert '"verdict"' in html                     # each node carries its computed verdict
    assert "cell 0" in html                        # a node label present in the baked graph
    assert "__BUNDLE_SRC__" not in html and "__APP_JS_SRC__" not in html
    assert "__GRAPH__" not in html and "__PROJECT__" not in html


def test_render_cockpit_focus_scopes_to_upstream_cone(cs_conn, tmp_path, monkeypatch):
    # focus renders only the seed's upstream cone: the fake host reads both projects (4 nodes), but
    # note.txt's lineage is just c0 -> c1, so the cockpit has 2 nodes.
    monkeypatch.chdir(tmp_path)
    ns = _exec_kernel(_FakeHost(cs_conn))
    out = ns["render_cockpit"](focus="note.txt")
    assert out["status"] == "rendered" and out["nodes"] == 2
    assert out["scope"] == {"focus": "note.txt"} and out["files"] == ["cockpit.html"]


def test_render_cockpit_focus_unresolved(cs_conn, tmp_path, monkeypatch):
    # a focus that names nothing in the project reports it, not a silent full-graph render
    monkeypatch.chdir(tmp_path)
    ns = _exec_kernel(_FakeHost(cs_conn))
    out = ns["render_cockpit"](focus="nonexistent.csv")
    assert out["status"] == "focus_unresolved" and out["focus"] == "nonexistent.csv"


def test_render_cockpit_empty_focus_is_unresolved(cs_conn, tmp_path, monkeypatch):
    # an EMPTY focus ([] / "") is a request that matched nothing — must NOT silently render the
    # whole project (that's what `if focus is not None` guards vs the old `if focus`).
    monkeypatch.chdir(tmp_path)
    ns = _exec_kernel(_FakeHost(cs_conn))
    assert ns["render_cockpit"](focus=[])["status"] == "focus_unresolved"
    assert ns["render_cockpit"](focus="")["status"] == "focus_unresolved"
    assert ns["render_cockpit"](focus=0)["status"] == "focus_unresolved"   # off-contract; no crash


def test_inlined_kernel_review_subgraph(cs_conn):
    # review_subgraph reads the seed's upstream cone and returns core.review_kit's brief.
    # note.txt's lineage is c0 -> c1, so nodes == 2 (the same cone render_cockpit(focus) reads).
    ns = _exec_kernel(_FakeHost(cs_conn))
    kit = ns["review_subgraph"]("note.txt")
    assert kit["scope"] == "upstream" and kit["nodes"] == 2
    assert "note.txt" in kit["focus"]
    for key in ("flags", "lineage", "artifacts", "boundary", "cells", "question"):
        assert key in kit


def test_inlined_kernel_review_subgraph_unresolved(cs_conn):
    # nodes that name nothing in the project report it, not a silent empty brief
    ns = _exec_kernel(_FakeHost(cs_conn))
    assert ns["review_subgraph"]("nonexistent.csv")["status"] == "seeds_unresolved"


def test_inlined_kernel_review_chat(cs_conn, tmp_path, monkeypatch):
    # review_chat resolves the current conversation from the CWD basename (= the frame id), seeds
    # from what THIS chat produced, and returns review_kit's brief. fd041418 made stats.csv ->
    # note.txt, so its chat cone is the same 2 nodes review_subgraph('note.txt') reads.
    chat = tmp_path / "fd041418"      # CWD basename = the current frame id (chat fd041418)
    chat.mkdir()
    monkeypatch.chdir(chat)
    ns = _exec_kernel(_FakeHost(cs_conn))
    kit = ns["review_chat"]()
    assert kit["scope"] == "upstream" and kit["nodes"] == 2
    assert "note.txt" in kit["focus"] and kit["chat_scoped"] is True
    for key in ("flags", "lineage", "artifacts", "boundary", "cells", "question"):
        assert key in kit


def test_inlined_kernel_review_chat_no_current_chat(cs_conn, tmp_path, monkeypatch):
    # a CWD whose basename matches no frame -> can't resolve the conversation; say so, don't
    # silently fall back to a whole-project review under a chat-scoped name.
    monkeypatch.chdir(tmp_path)       # random pytest tmp name -> matches no frame
    ns = _exec_kernel(_FakeHost(cs_conn))
    assert ns["review_chat"]()["status"] == "no_current_chat"


def test_inlined_kernel_review_chat_empty(cs_conn, tmp_path, monkeypatch):
    # a real conversation that produced nothing -> chat_empty (not a spurious brief). f_empty is a
    # frame with no artifact versions.
    chat = tmp_path / "f_empty"
    chat.mkdir()
    monkeypatch.chdir(chat)
    ns = _exec_kernel(_FakeHost(cs_conn))
    out = ns["review_chat"]()
    assert out["status"] == "chat_empty" and out["root_frame"] == "f_empty"


def test_review_chat_project_is_frame_derived_not_recency(cs_conn, tmp_path, monkeypatch):
    # the deterministic-project fix: the current project comes from the FRAME, not the recency
    # heuristic. f_empty_up is a proj_upload chat; proj_smoke (updated_at 200 > 100) is the recency
    # winner. So the reported project == proj_upload PROVES it was resolved from the frame — a
    # regression that fell back to recency would report proj_smoke here.
    chat = tmp_path / "f_empty_up"
    chat.mkdir()
    monkeypatch.chdir(chat)
    out = _exec_kernel(_FakeHost(cs_conn))["review_chat"]()
    assert out["status"] == "chat_empty" and out["project"] == "proj_upload"


class _FramesQueryRaises(_FakeHost):
    """A host whose ``frames`` lookup fails — stands in for a legacy operon without root_frame_id /
    project_id columns (the _current_frame try/except path)."""

    def query(self, sql, scope=None):
        if "FROM frames" in sql:
            raise RuntimeError("no such column: root_frame_id")
        return super().query(sql, scope)


def test_review_chat_degrades_when_frame_lookup_fails(cs_conn, tmp_path, monkeypatch):
    # _current_frame's try/except must swallow a frames-query error and return (None, None) so
    # review_chat declines gracefully (no_current_chat) instead of propagating the exception.
    chat = tmp_path / "fd041418"
    chat.mkdir()
    monkeypatch.chdir(chat)
    out = _exec_kernel(_FramesQueryRaises(cs_conn))["review_chat"]()
    assert out["status"] == "no_current_chat"   # graceful decline, not a crash


def test_review_chat_survives_getcwd_failure(cs_conn, monkeypatch):
    # _current_frame's docstring promises (None, None) on failure — a raising os.getcwd() (a deleted
    # or unmounted CWD) must degrade to no_current_chat, not propagate. Guards the getcwd-inside-try
    # fix: with getcwd outside the guard, this would raise instead.
    import os

    def _boom():
        raise OSError("cwd gone")

    monkeypatch.setattr(os, "getcwd", _boom)
    out = _exec_kernel(_FakeHost(cs_conn))["review_chat"]()
    assert out["status"] == "no_current_chat"


def test_render_cockpit_full_scope_is_uniform_dict(cs_conn, tmp_path, monkeypatch):
    # scope is always a dict; a full render carries focus=None (not the string "full")
    monkeypatch.chdir(tmp_path)
    ns = _exec_kernel(_FakeHost(cs_conn))
    assert ns["render_cockpit"]()["scope"] == {"focus": None}


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
    # marker, CSS pulled out of the shell into app_js, PG:SKILL-DATA + fallback injected in order,
    # snapshot on. Backstops the transform the render tests stub out.
    shell, app_js = build_skill._skill_cockpit_html()
    assert "PG:VENDOR" not in shell                                  # inlined vendor stripped
    assert shell.count('<script src="__BUNDLE_SRC__">') == 1         # bundle marker inserted once
    assert shell.count('<script src="__APP_JS_SRC__">') == 1         # app-js marker inserted once
    assert shell.count("__GRAPH__") == 1 and shell.count("__PROJECT__") == 1
    assert "app CSS is injected by cockpit-app.js" in shell         # <style> pulled from the shell
    assert "PG:SKILL-DATA" in shell and "pg-skill-fallback" in shell
    assert "window.__PG_SNAPSHOT" in shell                          # snapshot mode activated in-CS
    # order: bundle marker in <head>, data before the app-js marker, fallback before </body>
    assert (shell.index("__BUNDLE_SRC__") < shell.index("PG:SKILL-DATA")
            < shell.index("__APP_JS_SRC__") < shell.index("pg-skill-fallback"))
    # app_js carries the CSS (injected as a <style> on load) + the real app (PG:DATA-IO)
    assert "document.createElement('style')" in app_js and "PG:DATA-IO" in app_js


def test_render_outputs_are_all_excluded(cs_conn, tmp_path, monkeypatch):
    # drift guard: every file render_cockpit save_artifacts must be one the reader self-excludes
    from provenance_gate.adapters.cs_skill.host_query_reader import SELF_ARTIFACTS
    monkeypatch.chdir(tmp_path)
    first = _exec_kernel(_FakeHost(cs_conn, assets_saved=False))["render_cockpit"]()   # unsaved
    second = _exec_kernel(_FakeHost(cs_conn, assets_saved=True))["render_cockpit"]()
    assert set(first["files"]) | set(second["files"]) == set(SELF_ARTIFACTS)
