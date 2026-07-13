"""Shared synthetic operon fixtures — one source of truth for capture and checkpoint tests.

Hand-built rows mirroring the operon subset the harness reads (projects, artifacts,
artifact_versions, artifact_dependencies). Deterministic; no external database.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

SCHEMA = """
CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE artifacts(
    id TEXT PRIMARY KEY, project_id TEXT, filename TEXT, latest_version_id TEXT);
CREATE TABLE artifact_versions(
    id TEXT PRIMARY KEY, artifact_id TEXT, version_number INTEGER,
    parent_version_id TEXT, checksum TEXT);
CREATE TABLE artifact_dependencies(
    id TEXT PRIMARY KEY, artifact_version_id TEXT, depends_on_version_id TEXT, reference_name TEXT);
"""

# The transcript half of the operon that the harness reads back for prose and approvals. Kept apart
# from SCHEMA so existing artifact-only fixtures stay unchanged; a root frame points at itself and
# each message row carries the same JSON the app persists.
FRAME_SCHEMA = """
CREATE TABLE frames(
    id TEXT PRIMARY KEY, root_frame_id TEXT, project_id TEXT, status TEXT, model TEXT);
CREATE TABLE frame_messages(
    frame_id TEXT, idx INTEGER, msg_json TEXT, msg_uuid TEXT, PRIMARY KEY(frame_id, idx));
"""


def frame_message(role: str, message_id: str, content: list[dict[str, object]]) -> str:
    """Encode one operon frame message: role, content blocks, and the message's embedded uuid."""
    return json.dumps({"role": role, "content": content, "_uuid": message_id})


class Operon:
    """In-memory operon fixture: artifacts carry versions; a version records what it read."""

    def __init__(self, project_id: str = "proj_test"):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.pid = project_id
        self.conn.execute("INSERT INTO projects VALUES(?,?)", (project_id, "test"))
        self._seq = 0

    def artifact(self, filename: str) -> str:
        aid = f"a_{filename}"
        self.conn.execute(
            "INSERT INTO artifacts(id, project_id, filename, latest_version_id) VALUES(?,?,?,NULL)",
            (aid, self.pid, filename),
        )
        return aid

    def version(
        self,
        artifact_id: str,
        number: int,
        *,
        reads: list[str] | None = None,
        latest: bool = False,
        checksum: str | None = None,
    ) -> str:
        vid = f"v_{artifact_id}_{number}"
        self.conn.execute(
            "INSERT INTO artifact_versions(id, artifact_id, version_number, checksum) "
            "VALUES(?,?,?,?)",
            (vid, artifact_id, number, checksum or hashlib.sha256(vid.encode()).hexdigest()),
        )
        if latest:
            self.conn.execute(
                "UPDATE artifacts SET latest_version_id=? WHERE id=?", (vid, artifact_id)
            )
        for input_vid in reads or []:
            self._seq += 1
            self.conn.execute(
                "INSERT INTO artifact_dependencies VALUES(?,?,?,?)",
                (f"d_{self._seq}", vid, input_vid, None),
            )
        return vid


def diamond(op: Operon) -> tuple[str, str, str]:
    """The toy version-mix diamond; returns (qc v1 id, qc v2 id, merge version id)."""
    cells = op.artifact("cells.csv")
    qc = op.artifact("cells.qc.csv")
    comp = op.artifact("composition.csv")
    sig = op.artifact("signature.csv")
    merged = op.artifact("combined_report.csv")
    cv = op.version(cells, 1, latest=True)
    qc1 = op.version(qc, 1, reads=[cv])
    qc2 = op.version(qc, 2, reads=[cv], latest=True)
    comp1 = op.version(comp, 1, reads=[qc1], latest=True)   # Branch A pins qc v1
    sig1 = op.version(sig, 1, reads=[qc2], latest=True)     # Branch B pins qc v2
    merge = op.version(merged, 1, reads=[comp1, sig1], latest=True)
    return qc1, qc2, merge


def pbmc(op: Operon, *, conflict: bool = True) -> None:
    """Build the PBMC figure-package construction. With ``conflict`` (default) a stricter-QC
    revision re-versions cells.qc.csv and regenerates only the IFN branch, so the final package
    reconverges on cells.qc.csv v1 (composition/cytotoxic/qc) and v2 (ifn). With ``conflict=False``
    (clean control) the IFN branch is recomputed under the SAME QC, so everything stays on v1.
    """
    cells = op.artifact("cells.csv")
    qc_params = op.artifact("qc_params.csv")
    qc = op.artifact("cells.qc.csv")
    composition = op.artifact("composition.csv")
    ifn = op.artifact("ifn_signature.csv")
    cyto = op.artifact("cytotoxic_signature.csv")
    qc_summary = op.artifact("qc_summary.csv")
    p_comp = op.artifact("panel_composition.csv")
    p_ifn = op.artifact("panel_ifn.csv")
    p_cyto = op.artifact("panel_cytotoxic.csv")
    p_qc = op.artifact("panel_qc.csv")
    style = op.artifact("figure_style.csv")
    manifest = op.artifact("figure_manifest.csv")

    cells_v1 = op.version(cells, 1, latest=True)
    qcp_v1 = op.version(qc_params, 1, latest=True)
    qc_v1 = op.version(qc, 1, reads=[cells_v1, qcp_v1], latest=True)
    comp_v1 = op.version(composition, 1, reads=[qc_v1], latest=True)
    ifn_v1 = op.version(ifn, 1, reads=[qc_v1])
    cyto_v1 = op.version(cyto, 1, reads=[qc_v1], latest=True)
    qcs_v1 = op.version(qc_summary, 1, reads=[qc_v1], latest=True)
    pcomp_v1 = op.version(p_comp, 1, reads=[comp_v1], latest=True)
    pifn_v1 = op.version(p_ifn, 1, reads=[ifn_v1])
    pcyto_v1 = op.version(p_cyto, 1, reads=[cyto_v1], latest=True)
    pqc_v1 = op.version(p_qc, 1, reads=[qcs_v1], latest=True)
    panels_v1 = [pcomp_v1, pifn_v1, pcyto_v1, pqc_v1]
    op.version(style, 1)
    op.version(manifest, 1, reads=panels_v1)
    op.version(style, 2, latest=True)                       # cosmetic-only re-version
    op.version(manifest, 2, reads=panels_v1, latest=True)

    if conflict:
        qcp_v2 = op.version(qc_params, 2, latest=True)       # stricter QC
        qc_source = op.version(qc, 2, reads=[cells_v1, qcp_v2], latest=True)  # cells.qc.csv v2
    else:
        qc_source = qc_v1                                    # clean control: same QC, no re-version

    ifn_v2 = op.version(ifn, 2, reads=[qc_source], latest=True)   # only the IFN branch regenerates
    op.version(p_ifn, 2, reads=[ifn_v2], latest=True)             # panel_ifn v2 <- ifn_signature v2
    # figure_values_final / figure_manifest_final are the TRIAL output, not construction — the agent
    # assembles them (a MISS) or refuses (a CATCH); item F scores that post-trial snapshot with the
    # oracle, so final-package reconvergence is not a construction gate.
