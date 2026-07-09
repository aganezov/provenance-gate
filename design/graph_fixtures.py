"""Generate design/graph_fixtures.json — real /api-shaped snapshots for the cockpit mock harness.

Reads Claude Science **read-only** via the gate's own substrate and dumps a handful of representative
project graphs (richest DAG, a small one, an empty one, a mid-size one) plus the full project list,
in the exact shape `/api/projects` and `/api/graph` return. Re-run after CS changes:

    uv run python design/graph_fixtures.py
"""

# ruff: noqa: E501 — a small utility script; a few data-massaging lines run long

from __future__ import annotations

import dataclasses
import json
import pathlib

from provenance_gate.adapters.external import server, substrate

OUT = pathlib.Path(__file__).resolve().parent / "graph_fixtures.json"
# a hand-built showcase project (frames + fan-out/in + a diamond) to always bake in, regardless of size
SHOWCASE = "complex project v1"


def main() -> None:
    cs = substrate.open_cs_db(server.default_cs_db())
    try:
        projects = substrate.list_projects(cs)
        sized = sorted(
            ((len(substrate.read_project_graph(cs, p["id"]).nodes), p["id"], p["name"]) for p in projects),
            reverse=True,
        )
        picks: dict[str, str] = {}  # project_id -> why it was picked
        if sized:
            picks[sized[0][1]] = "richest"
        for n, pid, _ in sized:  # a small, human-legible one
            if 2 <= n <= 4 and pid not in picks:
                picks[pid] = "small"
                break
        for n, pid, _ in reversed(sized):  # an empty one, to exercise the empty state
            if n == 0 and pid not in picks:
                picks[pid] = "empty"
                break
        if sized:  # a mid-size one
            picks.setdefault(sized[len(sized) // 2][1], "mid")
        for p in projects:  # always include the hand-built showcase (frames + fan-out/in), whatever its size
            if SHOWCASE.lower() in p["name"].lower():
                picks[p["id"]] = "showcase"
                break

        graphs = {pid: dataclasses.asdict(substrate.read_project_graph(cs, pid)) for pid in picks}
        # surface the picked (interesting) projects first in the dropdown
        picked_first = [p for p in projects if p["id"] in picks] + [p for p in projects if p["id"] not in picks]
        OUT.write_text(json.dumps({"projects": picked_first, "graphs": graphs}, indent=1))

        names = {p["id"]: p["name"] for p in projects}
        print(f"wrote {OUT.name}: {len(projects)} projects, {len(graphs)} graph snapshots")
        for pid, why in picks.items():
            print(f"  {why:8} {pid}  {names[pid]!r}  ({len(graphs[pid]['nodes'])} nodes)")
    finally:
        cs.close()


if __name__ == "__main__":
    main()
