#!/usr/bin/env python3
"""Read Claude Science (CS / operon) provenance straight from its SQLite store.

This is the *fast machine-read* path: no browser, no LLM-turn-per-click. It opens
the per-org store **read-only** (`file:...?mode=ro`, so it never blocks or mutates
the running daemon) and reports the primitives a provenance consumer cares about:

  - sessions / frames        (UPLOADS + OPERON compute + REVIEWER advisory)
  - artifacts + versions     (frozen, checksummed)
  - the *consumes* DAG        (artifact_dependencies edges, with reference labels)
  - the faithfulness pin      (recompute sha256 on disk == recorded checksum?)
  - the cone of a version     (transitive ancestors) + conflict / currency signals
  - execution cells           (source/exit/files written)
  - advisory verification     (CS's non-blocking sonnet reviewer)

Usage:
  cs_provenance.py projects                 # list projects
  cs_provenance.py show <name-or-id>        # full provenance read-back for a project
  cs_provenance.py cone <version-id>        # transitive cone of one artifact version
  cs_provenance.py verify <name-or-id>      # just the faithfulness pins for a project

Flags:
  --db PATH     override the store path (else $CS_DB, else the active org)
  --json        machine-readable output
  --no-verify   skip on-disk sha256 recompute (faster; `show` only)

The store location is discovered from ~/.claude-science/active-org.json when not
given. Artifacts live in the sibling `artifacts/` dir; `storage_path` is relative
to it. Both facts are load-bearing and are the reason this reader is portable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

CS_HOME = Path(os.environ.get("CS_HOME", Path.home() / ".claude-science"))


# --------------------------------------------------------------------------- #
# store discovery + connection
# --------------------------------------------------------------------------- #
def find_db(explicit: str | None) -> Path:
    """Locate operon-cli.db: --db, then $CS_DB, then the active org, then a glob."""
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get("CS_DB"):
        return Path(os.environ["CS_DB"]).expanduser()
    active = CS_HOME / "active-org.json"
    if active.exists():
        org = json.loads(active.read_text()).get("org_uuid")
        cand = CS_HOME / "orgs" / org / "operon-cli.db"
        if cand.exists():
            return cand
    hits = sorted((CS_HOME / "orgs").glob("*/operon-cli.db"))
    if len(hits) == 1:
        return hits[0]
    raise SystemExit(
        "Could not locate operon-cli.db. Pass --db, set $CS_DB, or check "
        f"{CS_HOME}/orgs/*/operon-cli.db (found {len(hits)})."
    )


def connect(db: Path) -> sqlite3.Connection:
    # mode=ro (NOT immutable=1): the daemon may be writing via WAL; ro reads a
    # consistent snapshot without taking a lock.
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def artifacts_root(db: Path) -> Path:
    return db.parent / "artifacts"


# --------------------------------------------------------------------------- #
# faithfulness pin
# --------------------------------------------------------------------------- #
def verify_pin(root: Path, storage_path: str, checksum: str) -> tuple[str, str]:
    """Return (status, detail). status in {ok, MISMATCH, MISSING}."""
    f = root / storage_path
    if not f.exists():
        return "MISSING", str(f)
    h = hashlib.sha256()
    with f.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    return ("ok", digest) if digest == checksum else ("MISMATCH", digest)


# --------------------------------------------------------------------------- #
# queries
# --------------------------------------------------------------------------- #
def resolve_project(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM projects WHERE id = ? OR name = ? "
        "OR id LIKE ? OR name LIKE ? ORDER BY created_at DESC LIMIT 1",
        (key, key, f"%{key}%", f"%{key}%"),
    ).fetchone()
    if not row:
        raise SystemExit(f"No project matching {key!r}.")
    return row


def project_frames(conn, pid):
    return conn.execute(
        "SELECT id, parent_frame_id, agent_name, status, name, conversation_type, "
        "created_at FROM frames WHERE project_id = ? ORDER BY created_at",
        (pid,),
    ).fetchall()


def project_artifacts(conn, pid):
    return conn.execute(
        "SELECT id, filename, latest_version_id, is_user_upload, "
        "superseded_by_artifact_id FROM artifacts WHERE project_id = ? "
        "ORDER BY sort_order",
        (pid,),
    ).fetchall()


def artifact_versions(conn, artifact_id):
    return conn.execute(
        "SELECT id, artifact_id, version_number, checksum, storage_path, "
        "producing_cell_id, frame_id, content_type, size_bytes "
        "FROM artifact_versions WHERE artifact_id = ? ORDER BY version_number",
        (artifact_id,),
    ).fetchall()


def version_row(conn, vid):
    return conn.execute(
        "SELECT av.*, a.filename, a.latest_version_id, a.project_id "
        "FROM artifact_versions av JOIN artifacts a ON a.id = av.artifact_id "
        "WHERE av.id = ? OR av.id LIKE ?",
        (vid, f"{vid}%"),
    ).fetchone()


def deps_of(conn, version_id):
    """Direct consumes-edges: this version depends_on -> (label, upstream version)."""
    return conn.execute(
        "SELECT d.depends_on_version_id, d.reference_name, a.filename, "
        "av.version_number, av.artifact_id, a.latest_version_id "
        "FROM artifact_dependencies d "
        "JOIN artifact_versions av ON av.id = d.depends_on_version_id "
        "JOIN artifacts a ON a.id = av.artifact_id "
        "WHERE d.artifact_version_id = ?",
        (version_id,),
    ).fetchall()


def project_edges(conn, pid):
    return conn.execute(
        "SELECT c.filename AS consumer, cv.version_number AS cver, "
        "d.reference_name AS ref, u.filename AS upstream, uv.version_number AS uver "
        "FROM artifact_dependencies d "
        "JOIN artifact_versions cv ON cv.id = d.artifact_version_id "
        "JOIN artifacts c ON c.id = cv.artifact_id "
        "JOIN artifact_versions uv ON uv.id = d.depends_on_version_id "
        "JOIN artifacts u ON u.id = uv.artifact_id "
        "WHERE c.project_id = ? ORDER BY c.filename, cv.version_number",
        (pid,),
    ).fetchall()


def execution_cells(conn, frame_ids):
    if not frame_ids:
        return []
    q = ",".join("?" * len(frame_ids))
    return conn.execute(
        f"SELECT frame_id, cell_index, language, exit_status, files_written "
        f"FROM execution_log WHERE frame_id IN ({q}) ORDER BY frame_id, cell_index",
        frame_ids,
    ).fetchall()


def verifications(conn, root_frame_ids):
    if not root_frame_ids:
        return []
    q = ",".join("?" * len(root_frame_ids))
    return conn.execute(
        f"SELECT verdict, severity, claim, evidence FROM verification_checks "
        f"WHERE root_frame_id IN ({q}) ORDER BY created_at",
        root_frame_ids,
    ).fetchall()


def cone(conn, version_id: str) -> dict:
    """Transitive closure of consumes-edges from a version (its cone)."""
    start = version_row(conn, version_id)
    if not start:
        raise SystemExit(f"No artifact version matching {version_id!r}.")
    seen: dict[str, sqlite3.Row] = {}
    edges: list[dict] = []
    stack = [start["id"]]
    while stack:
        vid = stack.pop()
        for d in deps_of(conn, vid):
            up = d["depends_on_version_id"]
            edges.append(
                {
                    "from": vid,
                    "to": up,
                    "ref": d["reference_name"],
                    "upstream": f"{d['filename']}@v{d['version_number']}",
                }
            )
            if up not in seen:
                seen[up] = d
                stack.append(up)
    # conflict = >=2 versions of the same artifact present in the cone
    # (key on artifact_id for correctness; display by filename)
    by_artifact: dict[str, tuple] = {}
    for r in seen.values():
        fn, vs = by_artifact.setdefault(r["artifact_id"], (r["filename"], set()))
        vs.add(r["version_number"])
    conflicts = {fn: sorted(vs) for (fn, vs) in by_artifact.values() if len(vs) > 1}
    # currency = a cone version that isn't its artifact's latest
    stale = [
        f"{r['filename']}@v{r['version_number']}"
        for r in seen.values()
        if r["depends_on_version_id"] != r["latest_version_id"]
    ]
    return {
        "root": f"{start['filename']}@v{start['version_number']}",
        "root_id": start["id"],
        "edges": edges,
        "ancestors": len(seen),
        "conflicts": conflicts,
        "stale": stale,
    }


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_projects(conn, args):
    rows = conn.execute(
        "SELECT p.id, p.name, p.created_at, "
        "(SELECT COUNT(*) FROM frames f WHERE f.project_id = p.id) AS frames, "
        "(SELECT COUNT(*) FROM artifacts a WHERE a.project_id = p.id) AS artifacts "
        "FROM projects p ORDER BY p.created_at DESC"
    ).fetchall()
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return
    print(f"{'PROJECT ID':<24} {'FRAMES':>6} {'ARTIF':>6}  NAME")
    for r in rows:
        print(f"{r['id']:<24} {r['frames']:>6} {r['artifacts']:>6}  {r['name']}")


def cmd_show(conn, db, args):
    p = resolve_project(conn, args.project)
    frames = project_frames(conn, p["id"])
    arts = project_artifacts(conn, p["id"])
    root = artifacts_root(db)

    # assemble
    kids: dict[str, list] = {}
    roots = []
    for f in frames:
        if f["parent_frame_id"]:
            kids.setdefault(f["parent_frame_id"], []).append(f)
        else:
            roots.append(f)
    root_frame_ids = [f["id"] for f in roots] + [
        k["id"] for v in kids.values() for k in v
    ]

    art_out = []
    for a in arts:
        vers = []
        for v in artifact_versions(conn, a["id"]):
            entry = {
                "version": v["version_number"],
                "id": v["id"],
                "checksum": v["checksum"],
                "storage_path": v["storage_path"],
                "latest": v["id"] == a["latest_version_id"],
            }
            if not args.no_verify:
                status, detail = verify_pin(root, v["storage_path"], v["checksum"])
                entry["pin"] = status
                entry["pin_detail"] = detail
            vers.append(entry)
        art_out.append(
            {
                "id": a["id"],
                "filename": a["filename"],
                "is_user_upload": bool(a["is_user_upload"]),
                "superseded_by": a["superseded_by_artifact_id"],
                "versions": vers,
            }
        )

    edges = project_edges(conn, p["id"])
    cells = execution_cells(conn, root_frame_ids)
    checks = verifications(conn, [f["id"] for f in roots])

    if args.json:
        print(
            json.dumps(
                {
                    "project": {"id": p["id"], "name": p["name"]},
                    "frames": [dict(f) for f in frames],
                    "artifacts": art_out,
                    "edges": [dict(e) for e in edges],
                    "cells": [dict(c) for c in cells],
                    "verifications": [dict(c) for c in checks],
                },
                indent=2,
            )
        )
        return

    print(f"Project: {p['name']}  ({p['id']})")
    print("\nFrames (a CS session = UPLOADS + OPERON compute + REVIEWER advisory):")
    for f in roots:
        tag = f["agent_name"] or f["conversation_type"]
        print(f"  [{tag:<8}] {f['name'] or '-':<44} {f['status']:<10} {f['id'][:8]}")
        for k in kids.get(f["id"], []):
            print(
                f"      └─ [{k['agent_name'] or '?':<8}] "
                f"{k['name'] or '-':<40} {k['status']:<10} {k['id'][:8]}"
            )

    print("\nArtifacts (frozen, checksummed):")
    for a in art_out:
        up = " upload" if a["is_user_upload"] else ""
        sup = "  SUPERSEDED" if a["superseded_by"] else ""
        print(f"  {a['filename']}{up}{sup}")
        for v in a["versions"]:
            latest = " [latest]" if v["latest"] else ""
            pin = ""
            if "pin" in v:
                mark = {"ok": "PIN✓", "MISMATCH": "PIN✗", "MISSING": "PIN?"}[
                    v["pin"]
                ]
                pin = f"  {mark}"
            print(
                f"      v{v['version']}  {v['id'][:8]}  "
                f"sha={v['checksum'][:12]}…{pin}{latest}"
            )

    print("\nConsumes DAG (edges):")
    if edges:
        for e in edges:
            ref = f" [{e['ref']}]" if e["ref"] else ""
            print(
                f"  {e['consumer']}@v{e['cver']}{ref}  ←  "
                f"{e['upstream']}@v{e['uver']}"
            )
    else:
        print("  (none)")

    print("\nExecution cells:")
    for c in cells:
        wrote = ""
        if c["files_written"]:
            try:
                names = [Path(x["path"]).name for x in json.loads(c["files_written"])]
                wrote = "  wrote: " + ", ".join(names)
            except Exception:
                pass
        print(
            f"  {c['frame_id'][:8]} cell{c['cell_index']} "
            f"{c['language']:<6} {c['exit_status']:<4}{wrote}"
        )

    print("\nVerification checks (advisory, non-blocking):")
    if checks:
        for c in checks:
            print(f"  [{c['verdict']}] {c['severity'] or ''} {c['claim'] or ''}".rstrip())
    else:
        print("  (none)")


def cmd_cone(conn, args):
    result = cone(conn, args.version)
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"Cone of {result['root']}  ({result['ancestors']} ancestors)")
    for e in result["edges"]:
        ref = f" [{e['ref']}]" if e["ref"] else ""
        print(f"  {e['from'][:8]}  ←{ref}  {e['upstream']}")
    conf = ", ".join(f"{fn}(v{'/v'.join(map(str, vs))})"
                     for fn, vs in result["conflicts"].items())
    print(f"\nConflict (>=2 versions of one artifact in cone): "
          f"{'YES — ' + conf if conf else 'none'}")
    print(f"Currency (non-latest versions in cone): "
          f"{', '.join(result['stale']) if result['stale'] else 'all current'}")


def cmd_verify(conn, db, args):
    p = resolve_project(conn, args.project)
    root = artifacts_root(db)
    rows = conn.execute(
        "SELECT a.filename, av.version_number, av.checksum, av.storage_path "
        "FROM artifact_versions av JOIN artifacts a ON a.id = av.artifact_id "
        "WHERE a.project_id = ? ORDER BY a.filename, av.version_number",
        (p["id"],),
    ).fetchall()
    ok = bad = 0
    out = []
    for r in rows:
        status, detail = verify_pin(root, r["storage_path"], r["checksum"])
        ok += status == "ok"
        bad += status != "ok"
        out.append(
            {"filename": r["filename"], "version": r["version_number"],
             "status": status, "detail": detail}
        )
        if not args.json:
            mark = {"ok": "✓", "MISMATCH": "✗ MISMATCH", "MISSING": "? MISSING"}[
                status
            ]
            print(f"  {mark:<12} {r['filename']}@v{r['version_number']}")
    if args.json:
        print(json.dumps({"project": p["name"], "ok": ok, "bad": bad, "pins": out},
                         indent=2))
    else:
        print(f"\n{ok} ok, {bad} not-ok")
    return 1 if bad else 0


# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="Read CS provenance from operon-cli.db")
    ap.add_argument("--db", help="path to operon-cli.db (else $CS_DB / active org)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("projects", help="list projects")
    s = sub.add_parser("show", help="full provenance read-back for a project")
    s.add_argument("project")
    s.add_argument("--no-verify", action="store_true", help="skip sha256 recompute")
    s = sub.add_parser("cone", help="transitive cone of an artifact version")
    s.add_argument("version")
    s = sub.add_parser("verify", help="faithfulness pins for a project")
    s.add_argument("project")

    args = ap.parse_args(argv)
    db = find_db(args.db)
    conn = connect(db)
    try:
        if args.cmd == "projects":
            return cmd_projects(conn, args)
        if args.cmd == "show":
            return cmd_show(conn, db, args)
        if args.cmd == "cone":
            return cmd_cone(conn, args)
        if args.cmd == "verify":
            return cmd_verify(conn, db, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
