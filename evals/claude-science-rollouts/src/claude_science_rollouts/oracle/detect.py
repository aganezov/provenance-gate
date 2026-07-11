"""The two structural detectors — pure functions of the provenance tables; no content is read."""

from __future__ import annotations

import sqlite3

from .closure import upstream_closure
from .models import MixFinding, StaleFinding

# Currency: a consumption edge pins an input version that is not its artifact's current version,
# EXCLUDING an edge where the consumer is itself a revision of that same artifact (reading v1 to
# write v2 is supersession, not stale use).
_STALE = """
SELECT ia.filename         AS artifact,
       iv.version_number   AS pinned,
       head.version_number AS latest,
       ca.filename         AS consumer,
       cv.id               AS consumer_version
FROM artifact_dependencies d
JOIN artifact_versions iv   ON iv.id = d.depends_on_version_id
JOIN artifacts         ia   ON ia.id = iv.artifact_id
JOIN artifact_versions cv   ON cv.id = d.artifact_version_id
JOIN artifacts         ca   ON ca.id = cv.artifact_id
LEFT JOIN artifact_versions head ON head.id = ia.latest_version_id
WHERE ia.project_id = :pid
  AND ia.latest_version_id IS NOT NULL
  AND d.depends_on_version_id <> ia.latest_version_id
  AND cv.artifact_id <> iv.artifact_id
ORDER BY ia.filename, cv.id
"""

# Terminal versions: project versions with no in-project consumer. Scoping the subquery to this
# project's consumers stops a cross-project edge hiding a terminal node; the NULL filter keeps a
# NULL from turning ``NOT IN`` into UNKNOWN for every row.
_LEAVES = """
SELECT av.id
FROM artifact_versions av JOIN artifacts a ON a.id = av.artifact_id
WHERE a.project_id = :pid
  AND av.id NOT IN (
      SELECT d.depends_on_version_id
      FROM artifact_dependencies d
      JOIN artifact_versions cv ON cv.id = d.artifact_version_id
      JOIN artifacts         ca ON ca.id = cv.artifact_id
      WHERE ca.project_id = :pid
        AND d.depends_on_version_id IS NOT NULL
  )
ORDER BY av.id
"""

# One row per version of each artifact reaching a node. The mix is grouped in Python so each
# finding carries the exact pinned version ids the content cross-check needs, not just numbers.
_CLOSURE_VERSIONS = """
SELECT av.artifact_id, a.filename, av.id, av.version_number
FROM artifact_versions av JOIN artifacts a ON a.id = av.artifact_id
WHERE av.id IN ({placeholders}) AND av.version_number IS NOT NULL
ORDER BY a.filename, av.version_number, av.id
"""


def find_stale(conn: sqlite3.Connection, project_id: str) -> list[StaleFinding]:
    """Every consumption edge resting on a non-current version of its artifact (currency)."""
    return [
        StaleFinding(
            artifact=r["artifact"],
            pinned=r["pinned"],
            latest=r["latest"],
            consumer=r["consumer"],
            consumer_version=r["consumer_version"],
        )
        for r in conn.execute(_STALE, {"pid": project_id})
    ]


def find_version_mix(conn: sqlite3.Connection, project_id: str) -> list[MixFinding]:
    """For each terminal node, report any artifact its upstream closure carries at >1 version.
    A mix at an internal node also surfaces at every leaf below it, so scanning leaves catches
    every mix and attributes it to the terminal (merge) node where the versions land together."""
    findings: list[MixFinding] = []
    leaves = [row[0] for row in conn.execute(_LEAVES, {"pid": project_id})]
    for leaf in leaves:
        closure = upstream_closure(conn, project_id, leaf)
        if len(closure) < 2:
            continue
        sql = _CLOSURE_VERSIONS.format(placeholders=",".join(["?"] * len(closure)))
        by_artifact: dict[tuple[str, str], dict[int, str]] = {}
        for r in conn.execute(sql, tuple(closure)):
            by_artifact.setdefault((r["artifact_id"], r["filename"]), {}).setdefault(
                r["version_number"], r["id"]
            )
        for (artifact_id, filename), by_number in by_artifact.items():
            if len(by_number) < 2:
                continue
            numbers = sorted(by_number)
            findings.append(
                MixFinding(
                    artifact=filename,
                    artifact_id=artifact_id,
                    versions=tuple(numbers),
                    version_ids=tuple(by_number[n] for n in numbers),
                    merge_node=leaf,
                )
            )
    return findings
