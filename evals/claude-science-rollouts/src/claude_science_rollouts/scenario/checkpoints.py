"""Construction checkpoints — the structured assertion vocabulary that verifies a replicate's DAG
matches the authored construction (the label a replicate is scored against).

Each assertion is a typed structural predicate over the operon snapshot: version pins, dependency
edges, upstream-closure membership, checksum distinctness. Closure membership reuses the oracle's
``upstream_closure`` — one closure implementation, not two. Content-value assertions (row counts,
cell values) land with the artifact-content snapshot later; ``kind`` is the seam they attach onto.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from claude_science_rollouts.oracle import upstream_closure


@dataclass(frozen=True, slots=True)
class AssertionResult:
    kind: str
    ok: bool
    detail: str


MODES = frozenset({"gate", "measure"})


@dataclass(frozen=True, slots=True)
class CheckpointResult:
    id: str
    mode: str            # "gate" | "measure"
    passed: bool
    assertions: tuple[AssertionResult, ...]


def _resolve(conn: sqlite3.Connection, pid: str, filename: str, number: int) -> str | None:
    row = conn.execute(
        "SELECT av.id FROM artifact_versions av JOIN artifacts a ON a.id = av.artifact_id "
        "WHERE a.project_id = ? AND a.filename = ? AND av.version_number = ?",
        (pid, filename, number),
    ).fetchone()
    return row[0] if row else None


def _latest_number(conn: sqlite3.Connection, pid: str, filename: str) -> int | None:
    row = conn.execute(
        "SELECT head.version_number FROM artifacts a "
        "LEFT JOIN artifact_versions head ON head.id = a.latest_version_id "
        "WHERE a.project_id = ? AND a.filename = ?",
        (pid, filename),
    ).fetchone()
    return row[0] if row else None


def _direct_inputs(conn: sqlite3.Connection, version_id: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT depends_on_version_id FROM artifact_dependencies "
            "WHERE artifact_version_id = ? AND depends_on_version_id IS NOT NULL",
            (version_id,),
        )
    }


def _checksum(conn: sqlite3.Connection, version_id: str | None) -> str | None:
    if version_id is None:
        return None
    row = conn.execute(
        "SELECT checksum FROM artifact_versions WHERE id = ?", (version_id,)
    ).fetchone()
    return row[0] if row else None


def _assert(conn: sqlite3.Connection, pid: str, a: dict[str, Any]) -> AssertionResult:
    kind = a["kind"]
    if kind == "version_exists":
        ok = _resolve(conn, pid, a["artifact"], a["version"]) is not None
        return AssertionResult(kind, ok, f"{a['artifact']} v{a['version']}")
    if kind == "latest_version":
        got = _latest_number(conn, pid, a["artifact"])
        return AssertionResult(kind, got == a["version"], f"{a['artifact']} latest={got}")
    if kind == "depends_on":
        cv = _resolve(conn, pid, a["consumer"]["artifact"], a["consumer"]["version"])
        edges = _direct_inputs(conn, cv) if cv else set()
        missing = [
            f"{i['artifact']} v{i['version']}"
            for i in a["inputs"]
            if _resolve(conn, pid, i["artifact"], i["version"]) not in edges
        ]
        return AssertionResult(kind, cv is not None and not missing, "; ".join(missing) or "ok")
    if kind == "closure_contains":
        nv = _resolve(conn, pid, a["node"]["artifact"], a["node"]["version"])
        closure = upstream_closure(conn, pid, nv) if nv else set()
        missing = [
            f"{fn} v{n}"
            for fn, numbers in a["artifacts"].items()
            for n in numbers
            if _resolve(conn, pid, fn, n) not in closure
        ]
        return AssertionResult(kind, nv is not None and not missing, "; ".join(missing) or "ok")
    if kind in ("checksums_differ", "checksums_equal"):
        sums = [_checksum(conn, _resolve(conn, pid, a["artifact"], n)) for n in a["versions"]]
        distinct = len(set(sums)) == len(sums)
        ok = (distinct if kind == "checksums_differ" else len(set(sums)) == 1) and None not in sums
        return AssertionResult(kind, ok, f"{a['artifact']} {a['versions']}")
    raise ValueError(f"unknown checkpoint assertion kind: {kind!r}")


def evaluate_checkpoints(
    conn: sqlite3.Connection, project_id: str, checkpoints: list[dict[str, Any]]
) -> list[CheckpointResult]:
    """Evaluate each checkpoint's assertions; a checkpoint passes iff all its assertions pass."""
    results: list[CheckpointResult] = []
    for cp in checkpoints:
        mode = cp.get("mode", "gate")
        if mode not in MODES:
            raise ValueError(f"checkpoint {cp.get('id')!r} has unknown mode {mode!r}")
        ars = tuple(_assert(conn, project_id, a) for a in cp["assertions"])
        results.append(
            CheckpointResult(
                id=cp["id"],
                mode=mode,
                passed=all(ar.ok for ar in ars),
                assertions=ars,
            )
        )
    return results


def all_gates_pass(results: list[CheckpointResult]) -> bool:
    """Fail-closed construction integrity: scoreable only when at least one ``gate`` checkpoint ran
    AND every gate passed. An empty result set, a gate-less scenario, or an unknown mode is NOT
    scoreable — the denominator must never admit an unverified construction."""
    if any(r.mode not in MODES for r in results):
        raise ValueError("checkpoint result carries an unknown mode")
    gates = [r for r in results if r.mode == "gate"]
    return bool(gates) and all(r.passed for r in gates)
