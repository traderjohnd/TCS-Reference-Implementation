-- TCS PostgreSQL Migration 002 — Indexes
-- Mirrors the SQLite indexes for query performance.

CREATE INDEX IF NOT EXISTS idx_tc_chain        ON trust_certificates (chain_id, chain_sequence);
CREATE INDEX IF NOT EXISTS idx_tc_subject      ON trust_certificates (subject_id);
CREATE INDEX IF NOT EXISTS idx_tc_decision     ON trust_certificates (decision);
CREATE INDEX IF NOT EXISTS idx_tc_eval_time    ON trust_certificates (evaluation_timestamp);
CREATE INDEX IF NOT EXISTS idx_tc_policy       ON trust_certificates (policy_set_id);

CREATE INDEX IF NOT EXISTS idx_lifecycle_cert  ON lifecycle_events (certificate_id);

CREATE INDEX IF NOT EXISTS idx_metrics_name_time ON trust_metrics (metric_name, recorded_at);

CREATE INDEX IF NOT EXISTS idx_request_subject ON request_audit (subject_id);
CREATE INDEX IF NOT EXISTS idx_request_cert    ON request_audit (certificate_id);
CREATE INDEX IF NOT EXISTS idx_audit_request   ON request_audit (request_id);

CREATE INDEX IF NOT EXISTS idx_pa_profile      ON policy_adaptations (risk_tolerance_profile_id);
CREATE INDEX IF NOT EXISTS idx_pa_status       ON policy_adaptations (approval_status);

CREATE INDEX IF NOT EXISTS idx_ri_status       ON recovery_incidents (status);
