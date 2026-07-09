"""build_mock strip/round-trip: the greedy MOCK_RE survives a stray marker in baked data, and bake->strip
is lossless. Pure string logic — no CS, no cytoscape."""

# ruff: noqa: E501 — test fixtures inline HTML + PG markers that are naturally long

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "design"))
import build_mock  # noqa: E402


def test_strip_greedy_survives_stray_marker():
    # a baked fixture whose `code` contains the closing marker must NOT truncate the block early
    block = '\n// ===== PG:MOCK gen =====\nconst F={c:"// ===== /PG:MOCK ====="};\n// ===== /PG:MOCK =====\n'
    html = "A();\n// ===== /PG:LOG =====" + block + "APP();\nZ();\n"
    out = build_mock.strip_mock(html)
    assert "PG:MOCK" not in out  # greedy ran to the LAST marker, removed the whole block incl the stray
    assert "A();" in out and "APP();" in out and "Z();" in out  # surrounding app preserved


def test_bake_strip_roundtrip(tmp_path, monkeypatch):
    clean = "<!doctype html>\n<head></head>\n<body>\n<script>\n// ===== /PG:LOG =====\nrun();\n</script>\n</body>\n"
    (clean_p := tmp_path / "cockpit.html").write_text(clean)
    (fx := tmp_path / "fx.json").write_text(json.dumps({"projects": [], "graphs": {}}))
    monkeypatch.setattr(build_mock, "FIXTURES", fx)
    build_mock.bake(clean_p, mock_p := tmp_path / "mock.html")
    assert "PG:MOCK" in mock_p.read_text()
    build_mock.strip(mock_p, out_p := tmp_path / "out.html")
    assert out_p.read_text() == clean  # lossless round-trip
