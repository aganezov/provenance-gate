"""Smoke test: the harness package imports and exposes its version."""

import claude_science_rollouts


def test_package_imports():
    assert claude_science_rollouts.__version__ == "0.0.1"
