"""Read-only provenance utilities over a Claude Science operon snapshot.

Two things the harness reads Claude Science with, kept independent of the provenance gate so the
evaluation never leans on the gate's own code: a strictly read-only snapshot open/copy, and a
raw-SQL upstream-closure walk over consumption edges.
"""

from __future__ import annotations

from .closure import upstream_closure
from .snapshot import open_readonly, snapshot_operon

__all__ = [
    "upstream_closure",
    "snapshot_operon",
    "open_readonly",
]
