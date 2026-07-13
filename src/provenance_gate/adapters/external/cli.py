"""pg-audit: a one-shot provenance audit of an operon or a captured project snapshot.

Reads a Claude Science ``operon-cli.db`` — or a harness-captured ``project.db``, which is a
project-scoped operon of the same shape — derives the DAG, and prints the gate's per-cell trust
verdict. It runs the same read + audit the server (``pg-serve``) and the in-CS skill do, exposed as
a command so a captured rollout can be checked in a single line.
"""

from __future__ import annotations

import argparse
import json

from ...core.audit import audit_graph, flagged_verdicts
from ...core.review_kit import _name
from . import substrate


def _cell_labels(graph) -> dict:
    # name a cell by the artifacts it produces (falling back to its own label), so the report reads
    # in the reviewer's terms — "figure_final.png" rather than an opaque cell id. _name is the same
    # NULL-safe helper the review kit uses: CS keeps NULL-filename versions, and a bare None would
    # break the join — so both label builders share one fallback format.
    return {
        node.id: ", ".join(_name(ref) for ref in node.output_surface) or node.label
        for node in graph.nodes
    }


def _issue_line(issue: dict) -> str:
    used = "/".join(f"v{n}" for n in issue["versions"]) or "unversioned"
    current = f"current v{issue['current']}" if issue["current"] is not None else "no current head"
    return f"{issue['artifact']} ({used}; {current})"


def audit_db(db_path: str, project_id: str | None = None) -> dict:
    """Derive and audit one project in ``db_path``; return a JSON-safe report of the flagged cells.

    ``project_id`` defaults to the only project in the db (a captured snapshot holds exactly one).
    Raises ``ValueError`` if the db holds no project, so library callers can catch it.
    """
    reader = substrate.CsDbReader(db_path)
    projects = reader.list_projects()
    if not projects:
        raise ValueError(f"pg-audit: no project found in {db_path}")
    pid = project_id or projects[0]["id"]
    graph = reader.read_project_graph(pid)
    flags = flagged_verdicts(audit_graph(graph), _cell_labels(graph))
    return {"project": pid, "cells": len(graph.nodes), "flagged": flags}


def _print_report(report: dict) -> None:
    flags = report["flagged"]
    print(f"{report['project']}: {report['cells']} cells, {len(flags)} flagged")
    for flag in flags:
        print(f"  [{flag['verdict']:11}] {flag['cell']}")
        for kind, key in (("stale", "stale"), ("mix", "mixed")):
            for issue in flag[key]:
                print(f"                {kind}: {_issue_line(issue)}")
    if not flags:
        print("  clean — no stale or version-mixed lineage")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="One-shot provenance audit of an operon or a captured project snapshot."
    )
    ap.add_argument("db", help="path to an operon-cli.db or a captured project.db (read-only)")
    ap.add_argument("--project", help="project id (default: the only project in the db)")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = ap.parse_args()

    try:
        report = audit_db(args.db, args.project)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)
