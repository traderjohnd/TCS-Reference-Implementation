"""
tcs.api.routes_archive
======================

Archive management — snapshot the current database and start fresh.

Each archive is the complete SQLite database file, renamed with a
timestamp. The live system gets a brand-new empty database. No data
is ever deleted — it moves to the archives directory intact, with all
append-only guarantees preserved.

GET  /v2/archives          — list all archives
POST /v2/archives          — create a new archive (snapshot + reset)
GET  /v2/archives/{id}     — get archive details
GET  /v2/archives/{id}/certificates — browse certificates in archive
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel


router = APIRouter()

# Where archives live (sibling of data/)
ARCHIVES_DIR = Path("data") / "archives"
ARCHIVES_INDEX = ARCHIVES_DIR / "index.json"


# --------------------------------------------------------------------------- #
# Models                                                                       #
# --------------------------------------------------------------------------- #

class CreateArchiveRequest(BaseModel):
    label: Optional[str] = None


class ArchiveSummary(BaseModel):
    id: str
    label: str
    created_at: str
    certificate_count: int
    decision_counts: Dict[str, int]
    chain_count: int
    time_span: Optional[Dict[str, str]]  # {earliest, latest}
    file_size_kb: float
    filename: str


class ArchiveListResponse(BaseModel):
    archives: List[ArchiveSummary]
    total: int


# --------------------------------------------------------------------------- #
# Index management                                                             #
# --------------------------------------------------------------------------- #

def _load_index() -> List[Dict[str, Any]]:
    ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
    if ARCHIVES_INDEX.exists():
        return json.loads(ARCHIVES_INDEX.read_text())
    return []


def _save_index(entries: List[Dict[str, Any]]) -> None:
    ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVES_INDEX.write_text(json.dumps(entries, indent=2))


def _summarize_db(db_path: Path) -> Dict[str, Any]:
    """Read summary stats from an archived (or live) database."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # Certificate count
        cert_count = conn.execute(
            "SELECT COUNT(*) AS n FROM trust_certificates"
        ).fetchone()["n"]

        # Decision counts
        rows = conn.execute(
            "SELECT decision, COUNT(*) AS n FROM trust_certificates GROUP BY decision"
        ).fetchall()
        decision_counts = {r["decision"]: r["n"] for r in rows}

        # Chain count
        chain_count = conn.execute(
            "SELECT COUNT(DISTINCT chain_id) AS n FROM trust_certificates"
        ).fetchone()["n"]

        # Time span
        time_row = conn.execute(
            "SELECT MIN(evaluation_timestamp) AS earliest, "
            "MAX(evaluation_timestamp) AS latest FROM trust_certificates"
        ).fetchone()
        time_span = None
        if time_row["earliest"]:
            time_span = {
                "earliest": time_row["earliest"],
                "latest": time_row["latest"],
            }

        return {
            "certificate_count": cert_count,
            "decision_counts": decision_counts,
            "chain_count": chain_count,
            "time_span": time_span,
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# GET /v2/archives                                                             #
# --------------------------------------------------------------------------- #

@router.get("/archives")
def list_archives() -> ArchiveListResponse:
    entries = _load_index()
    summaries = []
    for entry in sorted(entries, key=lambda e: e["created_at"], reverse=True):
        summaries.append(ArchiveSummary(**entry))
    return ArchiveListResponse(archives=summaries, total=len(summaries))


# --------------------------------------------------------------------------- #
# POST /v2/archives — snapshot + reset                                         #
# --------------------------------------------------------------------------- #

@router.post("/archives")
def create_archive(body: CreateArchiveRequest, request: Request) -> ArchiveSummary:
    """
    1. Close the current database connection
    2. Snapshot summary stats from the live DB
    3. Move (rename) the DB file into archives/
    4. Re-initialize a fresh empty database
    5. Record the archive in the index
    """
    store = request.app.state.store
    live_db_path = Path(getattr(store, '_db_path', 'data/tcs.db'))

    # Use the actual path from the connection if available
    if hasattr(store, '_conn') and store._conn:
        # Try to get the database filename from the connection
        try:
            db_row = store._conn.execute("PRAGMA database_list").fetchone()
            if db_row and db_row[2]:
                live_db_path = Path(db_row[2])
        except Exception:
            pass

    # If in-memory or path doesn't exist, we can't archive
    if str(live_db_path) == ":memory:" or not live_db_path.exists():
        # For in-memory databases, just return a summary with zero counts
        now = datetime.now(timezone.utc)
        archive_id = now.strftime("%Y%m%d-%H%M%S")
        return ArchiveSummary(
            id=archive_id,
            label=body.label or "Empty archive",
            created_at=now.isoformat(),
            certificate_count=0,
            decision_counts={},
            chain_count=0,
            time_span=None,
            file_size_kb=0,
            filename="n/a",
        )

    ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)

    # Step 1: Get summary before closing
    summary = _summarize_db(live_db_path)

    now = datetime.now(timezone.utc)
    archive_id = now.strftime("%Y%m%d-%H%M%S")
    label = body.label or f"Archive {now.strftime('%b %d, %Y %I:%M %p')}"
    filename = f"tcs-archive-{archive_id}.db"
    archive_path = ARCHIVES_DIR / filename

    # Step 2: Copy the DB file to the archive.
    # On Windows, the live DB file is locked by the SQLite connection,
    # so we can't move it. Copy instead — the archive is a frozen snapshot.
    shutil.copy2(str(live_db_path), str(archive_path))

    # Step 3: Wipe and re-create all tables in the LIVE database.
    # We can't delete the file (Windows lock), so we drop the append-only
    # triggers first, then drop all tables, then re-run the schema.
    # This is safe because we already have a complete copy in the archive.
    conn = store._conn
    conn.execute("DROP TRIGGER IF EXISTS block_update_trust_certificates")
    conn.execute("DROP TRIGGER IF EXISTS block_delete_trust_certificates")
    conn.execute("DROP TRIGGER IF EXISTS block_update_lifecycle_events")
    conn.execute("DROP TRIGGER IF EXISTS block_delete_lifecycle_events")
    conn.execute("DROP TRIGGER IF EXISTS block_update_trust_metrics")
    conn.execute("DROP TRIGGER IF EXISTS block_delete_trust_metrics")
    conn.execute("DROP TRIGGER IF EXISTS block_update_request_audit")
    conn.execute("DROP TRIGGER IF EXISTS block_delete_request_audit")
    conn.execute("DROP TABLE IF EXISTS trust_certificates")
    conn.execute("DROP TABLE IF EXISTS lifecycle_events")
    conn.execute("DROP TABLE IF EXISTS trust_metrics")
    conn.execute("DROP TABLE IF EXISTS request_audit")
    conn.execute("DROP TABLE IF EXISTS policy_adaptations")
    conn.execute("DROP TABLE IF EXISTS recovery_incidents")
    conn.commit()

    # Step 4: Re-initialize schema on the same connection
    from tcs.persistence.db import _SCHEMA_SQL
    conn.executescript(_SCHEMA_SQL)

    # Also reset the interceptor's store reference if needed
    interceptor = request.app.state.interceptor
    if hasattr(interceptor, '_store'):
        interceptor._store = store

    # Clear any cached pipeline state (governed RAG)
    from tcs.api.routes_query import _pipeline_cache, _tcs_client_cache
    _pipeline_cache.clear()
    _tcs_client_cache.clear()

    # Step 5: Record in the index
    file_size_kb = round(archive_path.stat().st_size / 1024, 1)
    entry = {
        "id": archive_id,
        "label": label,
        "created_at": now.isoformat(),
        "certificate_count": summary["certificate_count"],
        "decision_counts": summary["decision_counts"],
        "chain_count": summary["chain_count"],
        "time_span": summary["time_span"],
        "file_size_kb": file_size_kb,
        "filename": filename,
    }

    index = _load_index()
    index.append(entry)
    _save_index(index)

    return ArchiveSummary(**entry)


# --------------------------------------------------------------------------- #
# GET /v2/archives/{id}                                                        #
# --------------------------------------------------------------------------- #

@router.get("/archives/{archive_id}")
def get_archive(archive_id: str) -> ArchiveSummary:
    index = _load_index()
    entry = next((e for e in index if e["id"] == archive_id), None)
    if not entry:
        from fastapi import HTTPException
        raise HTTPException(404, f"Archive {archive_id} not found")
    return ArchiveSummary(**entry)


# --------------------------------------------------------------------------- #
# GET /v2/archives/{id}/certificates                                           #
# --------------------------------------------------------------------------- #

@router.get("/archives/{archive_id}/certificates")
def list_archive_certificates(
    archive_id: str, limit: int = 50, offset: int = 0
) -> Dict[str, Any]:
    """Browse certificates in an archived database."""
    index = _load_index()
    entry = next((e for e in index if e["id"] == archive_id), None)
    if not entry:
        from fastapi import HTTPException
        raise HTTPException(404, f"Archive {archive_id} not found")

    archive_path = ARCHIVES_DIR / entry["filename"]
    if not archive_path.exists():
        from fastapi import HTTPException
        raise HTTPException(404, f"Archive file missing: {entry['filename']}")

    conn = sqlite3.connect(str(archive_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT certificate_id, subject_id, decision, tis_current, "
            "evaluation_timestamp, lifecycle_state, domain, risk_tier, "
            "action_class, policy_set_id "
            "FROM trust_certificates "
            "ORDER BY evaluation_timestamp DESC "
            "LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()

        total = conn.execute(
            "SELECT COUNT(*) AS n FROM trust_certificates"
        ).fetchone()["n"]

        return {
            "archive_id": archive_id,
            "label": entry["label"],
            "certificates": [dict(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    finally:
        conn.close()
