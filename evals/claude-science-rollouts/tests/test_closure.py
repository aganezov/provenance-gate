"""The kept upstream-closure utility follows consumption edges, not revision links.

Preserves the one negative invariant that lived in the removed oracle test suite: a linear revision
(v1 -> v2) is a parent link, not a dependency edge, so an older version of an artifact is never
reachable from a newer one through the closure.
"""

from __future__ import annotations

from claude_science_rollouts.oracle import upstream_closure
from operon_fixture import Operon


def test_upstream_closure_follows_dependencies_not_revisions():
    op = Operon()
    cells = op.artifact("cells.csv")
    qc = op.artifact("cells.qc.csv")
    cv = op.version(cells, 1, latest=True)
    op.version(qc, 1, reads=[cv])
    qc2 = op.version(qc, 2, reads=[cv], latest=True)  # v2 revises v1 but depends on cells, not v1
    assert upstream_closure(op.conn, op.pid, qc2) == {qc2, cv}  # qc v1 is NOT reachable
