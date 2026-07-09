"""Smoke test: proves the uv + pytest + editable-install toolchain works end to end."""

import provenance_gate


def test_package_imports():
    assert provenance_gate.__version__ == "0.0.1"
