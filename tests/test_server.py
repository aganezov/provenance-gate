"""server helper: the numeric version compare guarding the conditional-poll short-circuit."""

from provenance_gate.adapters.external.server import _same_version


def test_same_version_compares_numerically():
    assert _same_version("1751961600.5", 1751961600.5)
    assert _same_version("1751961600", 1751961600.0)  # integer string vs float — must still match
    assert _same_version("0", 0.0)
    assert not _same_version("", 5.0)   # first poll (no stored version) -> not "unchanged"
    assert not _same_version("0", 5.0)
    assert not _same_version("nan-ish", 5.0)
