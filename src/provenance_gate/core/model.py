"""Core object model for the Provenance Gate DAG.

Everything here is an immutable projection of Claude Science's read-only substrate:
a **node** is a computation, an **edge** is artifact consumption between computations.
Both are rebuilt from CS on demand — nothing here is authoritative on its own.

Deliberately absent for now (they attach later, keyed by the stable `Node.id`):
assumptions, surfaced values, human acts, cones, currency/conflict, verdict/color.

Two forward-proofing seams (see build-philosophy):
- ``frozen=True`` — nothing inside a node can alter (CS is read-only, artifacts frozen).
- a ``kind`` field on nodes and surface items — the wire discriminator that lets future
  variants (``"linked_value"`` on a surface, ``"merged"`` nodes) be *additive*, never a reshape.
``dataclasses.asdict`` emits ``kind`` for free, so no hand-written serializer is needed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """One item on a node's input/output surface: a reference to a frozen CS artifact version."""

    artifact_version_id: str  # stable CS id — the pin everything hangs off
    artifact_id: str
    version_number: int
    filename: str
    checksum: str  # sha256; unused now, present so faithfulness drops in later
    storage_path: str
    parent_version_id: str | None = None  # revision link; keeps "revision != conflict" recoverable
    kind: str = "artifact"  # wire discriminator seam (future: "linked_value")
    is_latest: bool = True  # current version of its artifact? drives the stale/mixed-version audit
    latest_version_id: str | None = None  # the artifact's current version id (self if is_latest)
    latest_version_number: int | None = None  # its number; UI shows "(current vN)" on stale chips


@dataclass(frozen=True, slots=True)
class Node:
    """An immutable computation derived from CS. Overlay attaches later by ``id``."""

    id: str  # stable, unique: producing cs_cell_id; a source node uses its artifact_version_id
    cs_project_id: str
    kind: str  # "computation" | "source"  (future: "merged")
    label: str  # computation → "cell N"; source → filename (the frame carries the task message)
    input_surface: tuple[ArtifactRef, ...] = ()  # artifacts it consumes
    output_surface: tuple[ArtifactRef, ...] = ()  # artifacts it produces
    # origin → CS (all None on a future merged node):
    cs_frame_id: str | None = None
    cs_cell_id: str | None = None
    cell_index: int | None = None
    code: str | None = None  # execution_log.source — the analysis, kept for later predicates


@dataclass(frozen=True, slots=True)
class Edge:
    """A directed artifact-consumption edge: ``src`` produced ``via``, ``dst`` consumes it."""

    id: str  # deterministic: f"{src_node_id}->{dst_node_id}:{via_artifact_version_id}"
    src_node_id: str
    dst_node_id: str
    via_artifact_version_id: str
    reference_name: str | None = None  # CS's label on the dependency


@dataclass(frozen=True, slots=True)
class Frame:
    """A CS *frame*: a task that groups cells. **Structural only** — frames carry no trust
    (no verdict, no overlay). A frame draws the bounding container and holds the task message the
    cells sit under, so a cell node need not repeat it. Nodes link to it via ``cs_frame_id``.
    """

    id: str  # stable CS frame id
    label: str  # the task message (CS ``frames.task_summary``); shown as the container title
    parent_frame_id: str | None = None  # frame hierarchy; unused in m0, captured for later nesting
    kind: str = "frame"  # wire discriminator seam


@dataclass(frozen=True, slots=True)
class Graph:
    """An immutable snapshot of one CS project's DAG: nodes + edges, plus the frames the nodes group
    under. All derived cache — nothing here carries trust."""

    cs_project_id: str
    nodes: tuple[Node, ...] = ()
    edges: tuple[Edge, ...] = ()
    frames: tuple[Frame, ...] = ()  # CS frames the nodes group under (container grouping; no trust)
    built_at: float = 0.0  # epoch seconds when this snapshot was derived
