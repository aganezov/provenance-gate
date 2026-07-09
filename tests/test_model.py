"""The object model is immutable, carries the `kind` wire seam, and serializes to plain JSON."""

import json
from dataclasses import FrozenInstanceError, asdict

import pytest

from provenance_gate.core.model import ArtifactRef, Edge, Graph, Node


def _artifact(**kw) -> ArtifactRef:
    base = dict(
        artifact_version_id="av1",
        artifact_id="a1",
        version_number=1,
        filename="stats.csv",
        checksum="219df1",
        storage_path="proj/av1/stats.csv",
    )
    base.update(kw)
    return ArtifactRef(**base)


def test_artifact_ref_carries_kind_discriminator():
    d = asdict(_artifact())
    assert d["kind"] == "artifact"  # the seam future variants branch on
    assert d["parent_version_id"] is None


def test_node_is_frozen():
    n = Node(id="n1", cs_project_id="proj", kind="computation", label="x")
    with pytest.raises(FrozenInstanceError):
        n.label = "y"  # nothing inside a node can alter


def test_graph_serializes_to_plain_json():
    a = _artifact()
    src = Node(
        id="c0", cs_project_id="proj", kind="computation",
        label="make stats.csv", output_surface=(a,),
    )
    dst = Node(
        id="c1", cs_project_id="proj", kind="computation",
        label="read stats.csv", input_surface=(a,),
    )
    e = Edge(
        id="c0->c1:av1", src_node_id="c0", dst_node_id="c1",
        via_artifact_version_id="av1", reference_name="stats.csv",
    )
    g = Graph(cs_project_id="proj", nodes=(src, dst), edges=(e,), built_at=123.0)

    d = asdict(g)
    json.dumps(d)  # must be JSON-serializable with no custom encoder

    assert len(d["nodes"]) == 2 and len(d["edges"]) == 1
    assert d["nodes"][0]["output_surface"][0]["filename"] == "stats.csv"
    assert d["nodes"][1]["input_surface"][0]["kind"] == "artifact"
    assert d["edges"][0]["reference_name"] == "stats.csv"
