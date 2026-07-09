"""Pure, deployment-agnostic core: the graph model, the derive logic, and the port contracts.

Imports nothing CS- or transport-specific. Every adapter (external sidecar, in-CS skill) hands
this core identical records and gets back the same immutable Graph — so the risky, valuable logic
is written and tested once, then reused by each surface. See ideas/ports-and-adapters-core.md.
"""
