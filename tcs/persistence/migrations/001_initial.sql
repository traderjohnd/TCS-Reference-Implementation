-- TCS PostgreSQL Migration 001 — Initial Schema
-- Mirrors the SQLite schema exactly. Same tables, same columns.
-- Append-only constraints implemented as RULEs.

CREATE TABLE IF NOT EXISTS trust_certificates (
    id                       SERIAL PRIMARY KEY,
    certificate_id           TEXT    NOT NULL UNIQUE,
    subject_id               TEXT    NOT NULL,
    subject_type             TEXT    NOT NULL,
    domain                   TEXT    NOT NULL,
    risk_tier                TEXT    NOT NULL,
    action_class             TEXT    NOT NULL,
    policy_set_id            TEXT    NOT NULL,
    decision                 TEXT    NOT NULL,
    lifecycle_state          TEXT    NOT NULL,
    invalidation_status      TEXT    NOT NULL DEFAULT 'valid',
    tis_raw                  DOUBLE PRECISION NOT NULL,
    tis_adjusted             DOUBLE PRECISION NOT NULL,
    tis_current              DOUBLE PRECISION NOT NULL,
    evaluation_timestamp     TEXT    NOT NULL,
    valid_until              TEXT    NOT NULL,
    tc_hash                  TEXT    NOT NULL UNIQUE,
    previous_tc_hash         TEXT,
    chain_id                 TEXT    NOT NULL,
    chain_sequence           INTEGER NOT NULL,
    hash_algorithm           TEXT    NOT NULL DEFAULT 'sha256',
    amended_tc_id            TEXT,
    content_json             TEXT    NOT NULL,
    inserted_at              TEXT    NOT NULL DEFAULT (NOW()::TEXT),

    UNIQUE (chain_id, chain_sequence)
);

CREATE TABLE IF NOT EXISTS lifecycle_events (
    id              SERIAL PRIMARY KEY,
    certificate_id  TEXT    NOT NULL,
    from_state      TEXT,
    to_state        TEXT    NOT NULL,
    reason          TEXT,
    occurred_at     TEXT    NOT NULL,
    inserted_at     TEXT    NOT NULL DEFAULT (NOW()::TEXT)
);

CREATE TABLE IF NOT EXISTS trust_metrics (
    id              SERIAL PRIMARY KEY,
    metric_name     TEXT    NOT NULL,
    metric_value    DOUBLE PRECISION NOT NULL,
    tags_json       TEXT,
    recorded_at     TEXT    NOT NULL DEFAULT (NOW()::TEXT)
);

CREATE TABLE IF NOT EXISTS request_audit (
    id                  SERIAL PRIMARY KEY,
    request_id          TEXT    NOT NULL UNIQUE,
    subject_id          TEXT,
    certificate_id      TEXT,
    decision            TEXT,
    fail_safe_applied   INTEGER NOT NULL DEFAULT 0,
    fail_safe_type      TEXT,
    received_at         TEXT    NOT NULL,
    resolved_at         TEXT,
    inserted_at         TEXT    NOT NULL DEFAULT (NOW()::TEXT)
);

-- Policy adaptation records (Phase 3)
CREATE TABLE IF NOT EXISTS policy_adaptations (
    id                       SERIAL PRIMARY KEY,
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
    created_at               TEXT    NOT NULL DEFAULT (NOW()::TEXT)
);

-- Recovery incidents (Phase 3)
CREATE TABLE IF NOT EXISTS recovery_incidents (
    id                       SERIAL PRIMARY KEY,
    incident_id              TEXT    NOT NULL UNIQUE,
    current_phase            TEXT    NOT NULL DEFAULT 'containment',
    status                   TEXT    NOT NULL DEFAULT 'active',
    trigger_d_trust          DOUBLE PRECISION NOT NULL,
    trigger_context          TEXT    NOT NULL,
    trigger_evidence_json    TEXT    NOT NULL,
    diagnostic_json          TEXT,
    remediation_json         TEXT,
    s_recovery               DOUBLE PRECISION,
    phase_history_json       TEXT    NOT NULL DEFAULT '[]',
    activated_at             TEXT    NOT NULL DEFAULT (NOW()::TEXT),
    completed_at             TEXT,
    updated_at               TEXT    NOT NULL DEFAULT (NOW()::TEXT)
);

-- Append-only rules for the four core tables.
-- PostgreSQL RULEs rewrite UPDATE/DELETE into no-ops.
DO $$ BEGIN
    CREATE RULE trust_certificates_no_update AS
        ON UPDATE TO trust_certificates DO INSTEAD NOTHING;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE RULE trust_certificates_no_delete AS
        ON DELETE TO trust_certificates DO INSTEAD NOTHING;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE RULE lifecycle_events_no_update AS
        ON UPDATE TO lifecycle_events DO INSTEAD NOTHING;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE RULE lifecycle_events_no_delete AS
        ON DELETE TO lifecycle_events DO INSTEAD NOTHING;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE RULE trust_metrics_no_update AS
        ON UPDATE TO trust_metrics DO INSTEAD NOTHING;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE RULE trust_metrics_no_delete AS
        ON DELETE TO trust_metrics DO INSTEAD NOTHING;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE RULE request_audit_no_update AS
        ON UPDATE TO request_audit DO INSTEAD NOTHING;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE RULE request_audit_no_delete AS
        ON DELETE TO request_audit DO INSTEAD NOTHING;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
