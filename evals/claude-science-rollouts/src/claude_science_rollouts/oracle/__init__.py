"""Independent structural oracle over a Claude Science operon snapshot.

The evaluation's structural ground truth, computed with its OWN raw-SQL closure walk. It never
imports the provenance gate, so the evaluation does not judge the gate with the gate's own code,
and it is strictly read-only over Claude Science.
"""

from __future__ import annotations

from .closure import upstream_closure
from .models import MixFinding, OracleVerdict, StaleFinding
from .snapshot import open_readonly, snapshot_operon
from .verdict import audit_project

__all__ = [
    "audit_project",
    "upstream_closure",
    "snapshot_operon",
    "open_readonly",
    "OracleVerdict",
    "MixFinding",
    "StaleFinding",
]
