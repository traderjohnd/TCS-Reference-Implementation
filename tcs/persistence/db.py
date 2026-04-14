"""
tcs.persistence.db
==================

Low-level SQLite schema and connection management for the Trust Certificate
archive.

The store is **append-only** by hard constraint: UPDATE and DELETE on the
``trust_certificates``, ``lifecycle_events``, ``trust_metrics``, and
``request_audit`` tables raise a SQLite trigger error. This satisfies:

    C-R.18 — Hash chain integrity (TC archive is append-only, deletion
             prohibited)
    C-P.14 — TC modification after issuance
    C-P.15 — TC deletion

Corrections are modeled as *amendment TCs* (new rows with an
``amended_tc_id`` reference) — never mutations of prior rows. This module
enforces the no-mutation half; the higher-level ``CertificateStore`` API
enforces the amendment-vs-mutation discipline.

Four tables:

    trust_certificates     — one row per TC; content_json + denormalized
                             index columns for fast lookup and chain walk
    lifecycle_events       — append-only state transitions
    trust_metrics          — counters and rolling statistics snapshots
    request_audit          — every governed request (even those whose TC
                             was never committed due to fail-safe)

Phase 2 uses SQLite with WAL mode for concurrent reads during writes.
Phase 3 will migrate this exact schema to PostgreSQL; all types are
chosen to be compatible with both engines.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional, Union


#: Default on-disk location for the TC archive. Test code uses :memory:
#: or a tmp_path fixture; demo code uses this.
DEFAULT_DB_PATH = Path("data") / "tcs.db"


class AppendOnlyViolation(sqlite3.IntegrityError):
    """
    Raised when an UPDATE or DELETE is attempted against an append-only
    table. Wraps the underlying sqlite3 trigger error so callers can
    catch a typed exception rather than string-matching IntegrityError.
    """


# --------------------------------------------------------------------------- #
# Schema                                                                       #
# --------------------------------------------------------------------------- #
#
# Notes on the schema shape:
#
# * ``content_json`` holds the full serialized TC produced by
#   ``tc.to_dict()``. Everything else is denormalized index material
#   pulled out of that JSON for fast queries. The ``tc_hash`` column is
#   UNIQUE — two different TC contents cannot share a hash without
#   breaking SHA-256.
#
# * ``chain_id`` + ``chain_sequence`` are UNIQUE together. Chains start
#   at sequence 1, strictly monotonic +1. Gaps are an integrity
#   violation surfaced by ``verify_chain()``.
#
# * ``previous_tc_hash`` is NULL for sequence=1 and NOT NULL thereafter.
#   We do not FK it back to ``tc_hash`` because a newly-issued TC may
#   reference a not-yet-committed previous hash during transaction
#   construction; integrity is checked at the application layer.
#
# * Triggers ``block_update_*`` and ``block_delete_*`` raise ABORT on any
#   row modification. They cannot be disabled by the application.

_SCHEMA_SQL = r"""
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS trust_certificates (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    certificate_id           TEXT    NOT NULL UNIQUE,
    subject_id               TEXT    NOT NULL,
    subject_type             TEXT    NOT NULL,
    domain                   TEXT    NOT NULL,
    risk_tier                TEXT    NOT NULL,
    action_class             TEXT    NOT NULL,
    policy_set_id            TEXT    NOT NULL,
    decision                 TEXT    NOT NULL,
    lifecycle_state          TEXT    NOT NULL,
    invalidation_status      TEXT    NOT NULL,
    tis_raw                  REAL    NOT NULL,
    tis_adjusted             REAL    NOT NULL,
    tis_current              REAL    NOT NULL,
    evaluation_timestamp     TEXT    NOT NULL,
    valid_until              TEXT    NOT NULL,
    tc_hash                  TEXT    NOT NULL UNIQUE,
    previous_tc_hash         TEXT,
    chain_id                 TEXT    NOT NULL,
    chain_sequence           INTEGER NOT NULL,
    hash_algorithm           TEXT    NOT NULL DEFAULT 'sha256',
    amended_tc_id            TEXT,
    content_json             TEXT    NOT NULL,
    inserted_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (chain_id, chain_sequence)
);

CREATE INDEX IF NOT EXISTS idx_tc_chain        ON trust_certificates (chain_id, chain_sequence);
CREATE INDEX IF NOT EXISTS idx_tc_subject      ON trust_certificates (subject_id);
CREATE INDEX IF NOT EXISTS idx_tc_decision     ON trust_certificates (decision);
CREATE INDEX IF NOT EXISTS idx_tc_eval_time    ON trust_certificates (evaluation_timestamp);

CREATE TABLE IF NOT EXISTS lifecycle_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    certificate_id  TEXT    NOT NULL,
    from_state      TEXT,
    to_state        TEXT    NOT NULL,
    reason          TEXT,
    occurred_at     TEXT    NOT NULL,
    inserted_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_cert ON lifecycle_events (certificate_id);

CREATE TABLE IF NOT EXISTS trust_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name     TEXT    NOT NULL,
    metric_value    REAL    NOT NULL,
    tags_json       TEXT,
    recorded_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_metrics_name_time ON trust_metrics (metric_name, recorded_at);

CREATE TABLE IF NOT EXISTS request_audit (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id          TEXT    NOT NULL UNIQUE,
    subject_id          TEXT,
    certificate_id      TEXT,
    decision            TEXT,
    fail_safe_applied   INTEGER NOT NULL DEFAULT 0,
    fail_safe_type      TEXT,
    received_at         TEXT    NOT NULL,
    resolved_at         TEXT,
    inserted_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_request_subject ON request_audit (subject_id);
CREATE INDEX IF NOT EXISTS idx_request_cert    ON request_audit (certificate_id);

-- Policy adaptation records (Phase 3 Step 4 — Policy Learning Layer).
-- Unlike the four TC tables, this table allows UPDATE on approval_status,
-- approver_identity, approval_timestamp, and applied_at columns only.
-- The core evidence and parameter_changes fields are immutable.

CREATE TABLE IF NOT EXISTS policy_adaptations (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id                TEXT    NOT NULL UNIQUE,
    triggered_by             TEXT    NOT NULL,
    risk_tolerance_profile_id TEXT   NOT NULL,
    parameter_changes_json   TEXT    NOT NULL,
    evidence_json            TEXT    NOT NULL,
    approval_status          TEXT    NOT NULL DEFAULT 'pending',
    approver_identity        TEXT,
    approval_timestamp       TEXT,
    applied_at               TEXT,
    rollback_available_until TEXT,
    created_at               TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_pa_profile ON policy_adaptations (risk_tolerance_profile_id);
CREATE INDEX IF NOT EXISTS idx_pa_status  ON policy_adaptations (approval_status);

-- Recovery incidents (Phase 3 Step 5 — Recovery Orchestrator).
-- Tracks the six-phase recovery lifecycle. Status updates are allowed
-- on phase, status, and diagnostic fields. Core trigger evidence is immutable.

CREATE TABLE IF NOT EXISTS recovery_incidents (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id              TEXT    NOT NULL UNIQUE,
    current_phase            TEXT    NOT NULL DEFAULT 'containment',
    status                   TEXT    NOT NULL DEFAULT 'active',
    trigger_d_trust          REAL    NOT NULL,
    trigger_context          TEXT    NOT NULL,
    trigger_evidence_json    TEXT    NOT NULL,
    diagnostic_json          TEXT,
    remediation_json         TEXT,
    s_recovery               REAL,
    phase_history_json       TEXT    NOT NULL DEFAULT '[]',
    activated_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    completed_at             TEXT,
    updated_at               TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_ri_status ON recovery_incidents (status);

-- Append-only triggers. Attempting UPDATE or DELETE on any of the four
-- append-only tables raises ABORT with a diagnostic message.

CREATE TRIGGER IF NOT EXISTS block_update_trust_certificates
BEFORE UPDATE ON trust_certificates
BEGIN
    SELECT RAISE(ABORT, 'trust_certificates is append-only (C-R.18, C-P.14)');
END;

CREATE TRIGGER IF NOT EXISTS block_delete_trust_certificates
BEFORE DELETE ON trust_certificates
BEGIN
    SELECT RAISE(ABORT, 'trust_certificates is append-only (C-R.18, C-P.15)');
END;

CREATE TRIGGER IF NOT EXISTS block_update_lifecycle_events
BEFORE UPDATE ON lifecycle_events
BEGIN
    SELECT RAISE(ABORT, 'lifecycle_events is append-only (C-R.18)');
END;

CREATE TRIGGER IF NOT EXISTS block_delete_lifecycle_events
BEFORE DELETE ON lifecycle_events
BEGIN
    SELECT RAISE(ABORT, 'lifecycle_events is append-only (C-R.18)');
END;

CREATE TRIGGER IF NOT EXISTS block_update_trust_metrics
BEFORE UPDATE ON trust_metrics
BEGIN
    SELECT RAISE(ABORT, 'trust_metrics is append-only (C-R.18)');
END;

CREATE TRIGGER IF NOT EXISTS block_delete_trust_metrics
BEFORE DELETE ON trust_metrics
BEGIN
    SELECT RAISE(ABORT, 'trust_metrics is append-only (C-R.18)');
END;

CREATE TRIGGER IF NOT EXISTS block_update_request_audit
BEFORE UPDATE ON request_audit
BEGIN
    SELECT RAISE(ABORT, 'request_audit is append-only (C-R.18)');
END;

CREATE TRIGGER IF NOT EXISTS block_delete_request_audit
BEFORE DELETE ON request_audit
BEGIN
    SELECT RAISE(ABORT, 'request_audit is append-only (C-R.18)');
END;
"""


# --------------------------------------------------------------------------- #
# Connection management                                                        #
# --------------------------------------------------------------------------- #


def _coerce_path(db_path: Union[str, Path, None]) -> str:
    """Accept ``None`` → default, ``":memory:"`` passthrough, or a filesystem path."""
    if db_path is None:
        return str(DEFAULT_DB_PATH)
    if isinstance(db_path, Path):
        return str(db_path)
    return db_path


def open_connection(
    db_path: Union[str, Path, None] = None,
) -> sqlite3.Connection:
    """
    Open a sqlite3 connection with sane Phase-2 defaults.

    - ``detect_types=PARSE_DECLTYPES`` so TEXT columns round-trip safely
    - ``isolation_level=None`` for explicit transaction control via BEGIN
    - ``check_same_thread=False`` so FastAPI worker threads can read/write
      the same store instance (the store is still single-threaded for
      *concurrent* writes — see CertificateStore docstring). Phase 3
      will replace this with a thread-safe connection pool.
    - row_factory set to :class:`sqlite3.Row` for dict-like access
    - foreign keys enabled on the per-connection pragma

    The caller is responsible for closing the connection (use a ``with``
    block via ``CertificateStore`` instead when possible).
    """
    path = _coerce_path(db_path)
    if path != ":memory:":
        parent = Path(path).parent
        if str(parent) and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # explicit transaction control
        check_same_thread=False,  # allow FastAPI worker thread dispatch
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(
    db_path: Union[str, Path, None] = None,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> sqlite3.Connection:
    """
    Initialize the TC archive schema.

    Either pass ``db_path`` (a new connection is opened and returned) or
    an existing ``conn`` (schema applied in-place; connection returned
    unchanged). Running this against an already-initialized database is
    idempotent — every ``CREATE`` uses ``IF NOT EXISTS``.

    Returns the connection so callers can chain further queries.
    """
    if conn is None:
        conn = open_connection(db_path)
    try:
        conn.executescript(_SCHEMA_SQL)
    except sqlite3.OperationalError:
        # WAL journal_mode is a best-effort pragma; if the platform does
        # not support it (e.g. :memory:), ignore and retry without it.
        conn.executescript(
            _SCHEMA_SQL.replace("PRAGMA journal_mode = WAL;", "")
        )
    return conn


# --------------------------------------------------------------------------- #
# Append-only error translation                                                #
# --------------------------------------------------------------------------- #

def translate_append_only_error(exc: sqlite3.IntegrityError) -> Exception:
    """
    If ``exc`` came from one of the block_update/block_delete triggers,
    return an :class:`AppendOnlyViolation` with the same message.
    Otherwise return ``exc`` unchanged.

    Use at call sites that might trigger an append-only block:

        try:
            conn.execute("UPDATE trust_certificates SET ...")
        except sqlite3.IntegrityError as e:
            raise translate_append_only_error(e) from e
    """
    msg = str(exc)
    if "append-only" in msg:
        return AppendOnlyViolation(msg)
    return exc
