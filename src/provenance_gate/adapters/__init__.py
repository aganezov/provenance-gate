"""Adapter columns — one per deployment surface, each implementing the core's ports.

``external`` is the standalone copilot (raw-DB read, sidecar SQLite, live HTTP cockpit); a future
``cs_skill`` column will be the in-CS native face (host.query/host.lineage, save_artifacts, a baked
cockpit snapshot). The core never learns which column it is running behind.
"""
