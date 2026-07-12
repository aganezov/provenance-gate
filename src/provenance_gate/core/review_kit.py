"""Review kit — a deterministic evidence brief over a lineage subgraph.

Given a Graph (a cone, read via ``read_graph(seeds)``), assemble a structured, agent-readable
package: what's under review, the deterministic flagged conflicts (reusing ``core.audit``), the
lineage as consumption edges, the artifact inventory, the raw-input boundary, and the cell ids to
fetch code for. The gate fixes WHAT the agent looks at — same graph in, same kit out — and the agent
does the reasoning, free to walk deeper with its own tools. The kit is a deterministic entry point,
not a leash.

Deliberately absent: the producing code (the agent fetches it — better tools for it) and any
attestation/seal (deferred). Pure over the Graph model: an adapter/skill resolves seeds, reads the
graph, then calls ``review_kit``. ``scope`` is a passthrough label for how the graph was pulled.
"""

from __future__ import annotations

from typing import Optional

from .audit import _ref_map, audit_graph, flagged_verdicts
from .model import Graph

REVIEW_QUESTION = (
    "Assess whether this result rests on coherent, current inputs; "
    "flag any silent version conflict or stale dependency."
)
GO_DEEPER = (
    "Fetch the producing code for these cells with your own tools; "
    "walk further upstream if you need more than this frame."
)


def _short(checksum: str) -> str:
    return checksum[:8] if checksum else ""


def _name(ref) -> str:
    # CS keeps NULL-filename versions; give them a stable, DISTINCT label (not a bare "") so a None
    # can't break sorts AND two unnamed terminals don't collapse to one entry in focus.
    return ref.filename or ("(unnamed " + ref.artifact_version_id[:8] + ")")


def _via_fields(ref, via_id: str) -> dict:
    """The ``artifact``/``version``/``checksum`` of an edge's via-version — the one shape shared by
    the lineage rows here and (inlined) by the skill's ``trusted_inputs``. ``ref`` is the resolved
    ArtifactRef or None; ``via_id`` is the raw version id, the label when it doesn't resolve."""
    return {"artifact": _name(ref) if ref else via_id,
            "version": ref.version_number if ref else None,
            "checksum": _short(ref.checksum) if ref else ""}


def review_kit(graph: Graph, scope: str = "upstream", verdicts: Optional[dict] = None) -> dict:
    """A deterministic review brief over ``graph`` (a lineage subgraph). Returns a JSON-safe dict;
    identical ``(graph, scope)`` produces an identical kit. Reuses ``core.audit`` for the flags.

    ``verdicts`` lets a caller supply per-node verdicts computed over a LARGER graph (e.g. the full
    ancestry cone) and then induce ``graph`` down to a selection: the flags then reflect that wider
    audit, restricted to the nodes actually in ``graph`` — narrowing the shown structure without
    narrowing the vigilance. Omit it and the flags are audited over ``graph`` itself (default)."""
    label = {n.id: n.label for n in graph.nodes}
    ref_of = _ref_map(graph)   # version_id -> ArtifactRef (the one map, shared with core.audit)

    # focus = the terminal RESULTS: artifact versions produced in the graph but consumed by nothing
    # in it. Per-VERSION, not per-node — a node with one consumed + one unconsumed output still
    # surfaces the unconsumed terminal.
    consumed = {e.via_artifact_version_id for e in graph.edges}
    focus = sorted({_name(a) for a in ref_of.values()
                    if a.artifact_version_id not in consumed})

    # default: audit this graph. Given verdicts (from a wider cone), restrict them to THIS graph's
    # nodes so the flags carry the wider audit's vigilance but only for the nodes we actually show.
    verds = (audit_graph(graph) if verdicts is None
             else {n: v for n, v in verdicts.items() if n in label})
    flags = flagged_verdicts(verds, label)   # shared serializer with audit_project

    lineage = []
    for e in graph.edges:
        row = {"from": label.get(e.src_node_id, e.src_node_id),
               "to": label.get(e.dst_node_id, e.dst_node_id)}
        row.update(_via_fields(ref_of.get(e.via_artifact_version_id), e.via_artifact_version_id))
        row["ref"] = e.reference_name
        lineage.append(row)
    lineage.sort(key=lambda r: (r["to"], r["from"], r["artifact"], r["version"] or 0))

    artifacts = sorted(
        ({"filename": _name(a), "version": a.version_number,
          "checksum": _short(a.checksum), "is_latest": a.is_latest}
         for a in ref_of.values()),
        key=lambda r: (r["filename"], r["version"] or 0),
    )

    # boundary = the raw inputs the lineage rests on (source nodes = no producer in the set)
    boundary = sorted(
        ({"artifact": _name(a), "version": a.version_number, "checksum": _short(a.checksum)}
         for n in graph.nodes if n.kind == "source" for a in n.output_surface),
        key=lambda r: (r["artifact"], r["version"] or 0),
    )

    cells = sorted((n.cs_cell_id if n.cs_cell_id is not None else n.id)
                   for n in graph.nodes if n.kind == "computation")

    return {
        "scope": scope,
        "focus": focus,
        "nodes": len(graph.nodes),
        "question": REVIEW_QUESTION,
        "flags": flags,
        "lineage": lineage,
        "artifacts": artifacts,
        "boundary": boundary,
        "cells": cells,
        "next": GO_DEEPER,
    }
