"""In-CS skill adapter column — the native face of the same core.

Runs inside a Claude Science skill (the repl / control-plane kernel): reads the project graph via
``host.query`` (raw sqlite is sandbox-blocked), derives with the *same* ``core.derive`` +
``core.audit`` the external surface uses, and (later) bakes a cockpit snapshot + persists findings
via ``save_artifacts``.

Grounded in the live ``merge-lineage-audit`` skill (host.query proven). Open probes before this
ships (see ideas/in-cs-skill-path.md §8): can a skill kernel import a vendored ``core`` package (vs
inline), ``save_artifacts`` from a custom skill, ``host.lineage`` in the repl kernel, host.query
auto-scoping.
"""
