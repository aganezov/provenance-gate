"""External-adjacent adapter column — the standalone copilot surface.

Reads Claude Science's operon DB directly (read-only), caches the derived graph in a per-project
sidecar SQLite, and serves the live cockpit over HTTP. This is the power tool: live updates and
(later) click-to-write triage — capabilities that live only here, never pushed down into the core.
"""
