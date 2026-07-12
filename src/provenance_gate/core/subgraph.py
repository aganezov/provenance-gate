"""Induced subgraph — restrict a derived Graph to a chosen set of nodes.

Pure ``Graph -> Graph``: given the node ids to keep, return the subgraph of exactly those nodes and
only the edges whose BOTH endpoints are kept (the *induced* subgraph). This is the seam behind a
selective review: the skill reads a node's full ancestry cone so the deterministic verdicts stay
honest (a version_mix can hide upstream), then induces down to just the picked nodes so the review
BRIEF shows only what the human selected — narrowing the *attention*, never the gate's *vigilance*.

Frames narrow to those the kept nodes sit in. Deterministic: node/edge/frame order is preserved from
``graph``, so the same ``(graph, keep)`` yields the same subgraph.
"""

from __future__ import annotations

from .model import Graph


def induced_subgraph(graph: Graph, keep_node_ids) -> Graph:
    """The subgraph of ``graph`` induced on ``keep_node_ids``: those nodes, and only the edges with
    BOTH endpoints kept. Frames narrow to the ones the kept nodes reference. Ids not in ``graph``
    are ignored; an empty keep set gives an empty graph (same project + built_at)."""
    keep = set(keep_node_ids)
    nodes = tuple(n for n in graph.nodes if n.id in keep)
    edges = tuple(e for e in graph.edges if e.src_node_id in keep and e.dst_node_id in keep)
    frame_ids = {n.cs_frame_id for n in nodes if n.cs_frame_id}
    frames = tuple(f for f in graph.frames if f.id in frame_ids)
    return Graph(cs_project_id=graph.cs_project_id, nodes=nodes, edges=edges,
                 frames=frames, built_at=graph.built_at)
