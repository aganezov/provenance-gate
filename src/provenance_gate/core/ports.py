"""The ports — interfaces the core owns; each adapter column implements them per deployment.

Five ports separate the deterministic core from every deployment detail:

    GraphReader   read a project's graph      external: raw-DB read   · skill: host.query + lineage
    OverlayStore  load/save the graph cache    external: sidecar SQLite · skill: save_artifacts
    Renderer      graph → cockpit HTML         external: serve live     · skill: bake a snapshot
    Control       trigger / gate / handoff     external: HTTP + watcher  · skill: MCP + pg_gate
    Llm           suggest assumptions / etc.   external: `claude -p`      · skill: host.llm

Only GraphReader and OverlayStore have clean Python method contracts, expressed below as
``Protocol``s — structural typing, so an adapter conforms just by matching the shape (no import
or subclassing needed). Renderer is realized by the data-driven ``ui/cockpit.html`` (fed live vs
baked); Control and Llm are realized by each surface's entrypoint. They are documented here as the
seam until they earn a Python contract of their own.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .model import Graph


@runtime_checkable
class GraphReader(Protocol):
    """Reads a CS project's immutable provenance graph. The port that differs most across surfaces
    (raw sqlite vs ``host.query``/``host.lineage``) — both must derive the *same* Graph from the
    *same* records, which is why the derive lives in ``core.derive`` and this port only fetches."""

    def list_projects(self) -> list[dict[str, str]]: ...

    def read_project_graph(self, project_id: str) -> Graph: ...


@runtime_checkable
class OverlayStore(Protocol):
    """Persists the derived cache today (and the owned overlay later). External = sidecar SQLite;
    the in-CS skill = a small ``findings.json`` artifact rebuilt-from-CS each run."""

    def replace_project_graph(self, graph: Graph) -> None: ...

    def load_graph(self, project_id: str) -> Graph: ...

    def close(self) -> None: ...
