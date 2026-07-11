"""Raw-SQL upstream closure over consumption edges — the oracle's independent traversal.

This is deliberately a recursive SQL CTE, not a Python graph walk: the evaluation must not
reuse the gate's subgraph extraction. It follows only ``artifact_dependencies`` (what a
computation consumed) and never ``artifact_versions.parent_version_id`` (the revision link).
That distinction is what makes a version collision meaningful — a linear revision (v1 -> v2) is
a parent link, not a dependency edge, so two versions of one artifact land in the same closure
only through a genuine divergent-branch reconvergence.
"""

from __future__ import annotations

import sqlite3

_UPSTREAM = """
WITH RECURSIVE up(vid) AS (
    SELECT :seed
    UNION
    SELECT d.depends_on_version_id
    FROM artifact_dependencies d
    JOIN up ON up.vid = d.artifact_version_id
    JOIN artifact_versions dv ON dv.id = d.depends_on_version_id
    JOIN artifacts da ON da.id = dv.artifact_id
    WHERE da.project_id = :pid AND d.depends_on_version_id IS NOT NULL
)
SELECT vid FROM up
"""


def upstream_closure(conn: sqlite3.Connection, project_id: str, seed_version_id: str) -> set[str]:
    """Every artifact_version reachable upstream of ``seed`` through consumption edges within
    ``project_id`` (the seed itself included). ``UNION`` dedups, so a cyclic graph still terminates.
    Scoping the recursive step to the project stops a cross-project edge from bridging the walk into
    unrelated versions.
    """
    rows = conn.execute(_UPSTREAM, {"seed": seed_version_id, "pid": project_id})
    return {row[0] for row in rows}
