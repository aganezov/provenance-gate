"""Project-level entry point: the oracle's structural verdict."""

from __future__ import annotations

import sqlite3

from .detect import find_stale, find_version_mix
from .models import OracleVerdict


def audit_project(conn: sqlite3.Connection, project_id: str) -> OracleVerdict:
    """Decide whether ``project_id`` contains a version-inconsistency, from provenance alone.

    ``inconsistent`` is true when some terminal node's lineage reconverges on two versions of one
    artifact (the merge conflict the slice measures). Currency (``stale``) is reported alongside
    but does not, by itself, set ``inconsistent`` — a superseded input that is never merged is a
    distinct defect from an inconsistent join.
    """
    mixed = tuple(find_version_mix(conn, project_id))
    stale = tuple(find_stale(conn, project_id))
    return OracleVerdict(project_id=project_id, inconsistent=bool(mixed), mixed=mixed, stale=stale)
