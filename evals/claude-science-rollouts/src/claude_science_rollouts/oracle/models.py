"""Result types for the independent structural oracle.

The oracle speaks its own verdict vocabulary and does not import the gate's model — the evaluation's
ground truth must stay independent of the code it evaluates.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StaleFinding:
    """A consumption edge that pins a non-current version of an artifact (a currency defect)."""

    artifact: str            # filename of the consumed artifact
    pinned: int | None       # version_number the consumer pinned
    latest: int | None       # the artifact's current version_number
    consumer: str            # filename of the consuming artifact
    consumer_version: str    # the consuming artifact_version id


@dataclass(frozen=True, slots=True)
class MixFinding:
    """A terminal node whose upstream closure consumes one artifact at two or more versions."""

    artifact: str                 # filename of the mixed artifact
    artifact_id: str
    versions: tuple[int, ...]     # the distinct version_numbers reaching the node
    version_ids: tuple[str, ...]  # the pinned artifact_version ids, aligned with ``versions``
    merge_node: str               # the terminal artifact_version id where the versions reconverge


@dataclass(frozen=True, slots=True)
class OracleVerdict:
    """Project-level structural ground truth. ``inconsistent`` tracks the version-mix (the merge
    conflict the slice measures); ``stale`` is the related currency signal, reported alongside."""

    project_id: str
    inconsistent: bool
    mixed: tuple[MixFinding, ...] = ()
    stale: tuple[StaleFinding, ...] = ()
