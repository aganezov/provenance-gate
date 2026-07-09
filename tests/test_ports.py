"""The ports are load-bearing, and the external adapters conform to them.

Structural (``runtime_checkable``) conformance is a weak check — it only sees method *names* — but
it catches a port method renamed without updating an adapter. The reader test also exercises
``CsDbReader`` directly (the GraphReader the app wires in), which the activation tests only hit
indirectly through ``get_fresh``.
"""

from provenance_gate.adapters.external.store import Store
from provenance_gate.adapters.external.substrate import CsDbReader
from provenance_gate.core.ports import GraphReader, OverlayStore


def test_cs_db_reader_is_a_graph_reader(cs_db_file):
    reader = CsDbReader(cs_db_file)
    assert isinstance(reader, GraphReader)  # conforms to the port
    assert {p["name"] for p in reader.list_projects()} >= {"drive-smoke-test", "upload-demo"}
    g = reader.read_project_graph("proj_smoke")
    assert {n.id for n in g.nodes} == {"c0", "c1"} and g.built_at > 0


def test_store_is_an_overlay_store():
    store = Store()  # in-memory
    try:
        assert isinstance(store, OverlayStore)
    finally:
        store.close()


def test_a_plain_object_is_not_a_graph_reader():
    assert not isinstance(object(), GraphReader)  # missing list_projects/read_project_graph
