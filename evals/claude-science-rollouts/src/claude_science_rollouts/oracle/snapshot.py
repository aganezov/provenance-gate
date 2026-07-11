"""Copy a live operon database into a run directory and open the frozen copy read-only.

Copy-then-open, for two reasons: the oracle must score an immutable point-in-time snapshot,
not a database still being written; and reading is strictly non-destructive to Claude Science —
only bytes are copied, the source is never opened for write.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path


def snapshot_operon(src_db: str | Path, dest_dir: str | Path) -> Path:
    """Copy the operon DB (plus any ``-wal``/``-shm`` sidecars) into ``dest_dir``, fold the WAL into
    the copy, and return the path to a single self-contained snapshot file.

    Folding the WAL and dropping the sidecars leaves one file that opens cleanly read-only. The
    checkpoint writes only to our copy — the source database is untouched.
    """
    src = Path(src_db)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dst = dest_dir / src.name
    for suffix in ("", "-wal", "-shm"):
        sidecar = Path(f"{src}{suffix}")
        if sidecar.exists():
            shutil.copyfile(sidecar, Path(f"{dst}{suffix}"))
    conn = sqlite3.connect(dst)
    try:
        # busy != 0 → not all WAL frames folded into the copy; dropping the sidecars would lose data
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row and row[0]:
            raise RuntimeError(
                f"WAL checkpoint on {dst} returned busy={row[0]}; snapshot may be incomplete"
            )
        conn.commit()
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        leftover = Path(f"{dst}{suffix}")
        if leftover.exists():
            leftover.unlink()
    return dst


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open ``db_path`` strictly read-only (URI ``mode=ro``), rows addressable by column name."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
