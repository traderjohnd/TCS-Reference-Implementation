// =============================================================================
// Shared certificate fixtures for Commit 6 component tests.
//
// V2 shapes mirror the live /v2/certificates/{id} wire after the tis-v2
// activation (canonical decimal STRINGS, gate_result int, flat typed
// rule matches, typed c3 provenance). The V1 shape mirrors a stored
// legacy certificate (JSON numbers, gate_passed boolean, nested rule
// `effect`, no certificate_schema_version on the wire).
//
// The protected-metadata 422 fixture is the REAL backend envelope,
// captured from the live FastAPI route (detail is an OBJECT for the
// violation; ordinary validation 422s carry a pydantic error ARRAY).
// =============================================================================

export const V2_CERT = {
  certificate_id: 'v2-cert-0001',
  subject_id: 'subject-v2-0001',
  subject_type: 'recommendation',
  domain: 'financial_services',
  risk_tier: 'r3',
  action_class: 'a4',
  policy_set_id: 'fin-r3-a4-ct4',
  certificate_schema_version: 2,
  calculation_version: 'tis-v2',
  score_precision_policy:
    'decimal-4dp-half-up-each-decision-stage-context28-v1',
  decay_algorithm_version:
    'decimal-exp-context28-half-even-then-4dp-half-up-v1',
  decision: 'Hold',
  requires_human_review: true,
  blocking_reason: 'attribution_gate_fail',
  lifecycle_state: 'computed',
  evaluation_timestamp: '2026-07-29T12:00:00Z',
  s_base: '0.9110',
  s_adjusted: '0.9010',
  tis_raw: '0.0000',
  tis_adjusted: '0.0000',
  tis_current: '0.0000',
  penalty_aggregate: '0.0110',
  c3_score: '1.0000',
  gate_result: 0,
  gate_results: { B: 'pass', A: 'fail', C: 'pass', K: 'pass' },
  component_scores_raw:
    { B: '0.95', A: '0.899996', C: '0.95', K: '0.88' },
  component_scores_observed:
    { B: '0.9500', A: '0.9000', C: '0.9500', K: '0.8800' },
  component_scores:
    { B: '0.9500', A: '0.9000', C: '0.9500', K: '0.8800' },
  component_weights:
    { B: '0.2500', A: '0.3000', C: '0.2500', K: '0.2000' },
  thresholds: { B: '0.9000', A: '0.9300', C: '0.9000', K: '0.8000' },
  adjustments_applied: [
    { rule_id: 'TCS_SPEC_19_1', dimension: 'B',
      value_before: '0.9400', value_after: '0.3000',
      reason: 'identity_confidence_below_0_30' },
    { rule_id: 'TCS_SPEC_19_2', dimension: 'B',
      value_before: '0.3000', value_after: '0.0000',
      reason: 'unverified_identity_on_T3_data' },
  ],
  c3_provenance: [],
  governance_rule_matches: [
    {
      schema_version: 1,
      rule_id: 'human_composed_patient_specific_medication_in_pregnancy',
      rule_version: 'v1',
      evaluator: 'typed_context',
      applies_to_domains: ['*'],
      matched_domain: 'medical_devices',
      matched_term_groups: [{ group_index: 0, term_index: 1 }],
      matched_fact_keys: ['channel', 'pregnant', 'role'],
      control_class: 'deterministic_bounded',
      safety_category: 'prohibited_action',
      c3_violation: false,
      blocking_reason:
        'patient_specific_medication_guidance_during_pregnancy',
      decision_pressure: 'HOLD',
      requires_human_review: true,
      boundedness_penalty: '0.0000',
      attribution_penalty: '0.0000',
      known_calibration_penalty: '0.0000',
      novelty_lift: '0.0000',
      explanation: 'Lithium is contraindicated in pregnancy.',
      active_policy_profile_id: 'meddev-pack',
    },
  ],
  scope_attestation: {},
  audit_integrity: {
    tc_hash: 'ab'.repeat(32), previous_tc_hash: null,
    chain_sequence: 1, chain_id: 'chain-test', hash_algorithm: 'sha256',
    integrity_verified: true, issued_by: 'tcs-test',
  },
};

// Same-bucket malformed v2: s_base carries a non-canonical string and
// gate_result a boolean. Must produce an integrity warning — never a
// silent coercion into the v1 presentation path.
export const V2_CERT_MALFORMED = {
  ...V2_CERT,
  certificate_id: 'v2-cert-malformed',
  subject_id: 'subject-v2-malformed',
  s_base: '0.911',          // not fixed-scale 4dp
  gate_result: true,        // not integer 0|1
};

export const V1_CERT = {
  certificate_id: 'v1-cert-0001',
  subject_id: 'subject-v1-0001',
  subject_type: 'recommendation',
  domain: 'financial_services',
  risk_tier: 'r3',
  action_class: 'a4',
  policy_set_id: 'fin-r3-a4-ct4',
  // no certificate_schema_version on the legacy wire — absence means v1
  decision: 'Allow',
  requires_human_review: false,
  blocking_reason: null,
  lifecycle_state: 'admissible',
  evaluation_timestamp: '2026-07-01T12:00:00Z',
  s_base: 0.9075,
  s_adjusted: 0.8806,
  tis_raw: 0.9075,
  tis_adjusted: 0.8806,
  tis_current: 0.8806,
  penalty_aggregate: 0.0296,
  gate_passed: true,
  gate_results: { B: 'pass', A: 'pass', C: 'pass', K: 'pass' },
  component_scores: { B: 0.94, A: 0.9, C: 0.92, K: 0.83 },
  component_weights: { B: 0.3, A: 0.25, C: 0.3, K: 0.15 },
  thresholds: { B: 0.9, A: 0.9, C: 0.9, K: 0.8 },
  governance_rule_matches: [
    {
      rule_id: 'legacy_rule',
      rule_version: 'v1',
      matched_facts: { pregnant: true },
      effect: {
        control_class: 'weighted_evidence',
        safety_category: 'advisory',
        override_policy: 'specialist_review',
        c3_category: 'none',
        explanation: 'Legacy nested effect shape.',
      },
    },
  ],
  scope_attestation: {},
  audit_integrity: {
    tc_hash: 'cd'.repeat(32), previous_tc_hash: null,
    chain_sequence: 1, chain_id: 'chain-legacy', hash_algorithm: 'sha256',
    integrity_verified: true, issued_by: 'tcs-test',
  },
};

export const UNSUPPORTED_CERT = {
  ...V1_CERT,
  certificate_id: 'v3-cert-unknown',
  certificate_schema_version: 3,
};

// Real backend contract — captured from the live FastAPI route.
export const PROTECTED_METADATA_422_BODY = {
  detail: {
    error: 'protected_metadata_keys',
    message:
      'extra_metadata may not supply governed scoring, gating, C3, '
      + 'validity, decision, identity, provenance, or enforcement keys. '
      + 'Identity attestations and evaluation typing (risk_tier, '
      + 'action_class, connection_type) use the typed request fields.',
    rejected_keys: ['nested.C_score', 'risk_tier'],
  },
};

export const ORDINARY_422_BODY = {
  detail: [
    { type: 'missing', loc: ['body', 'candidate_answer'],
      msg: 'Field required', input: { query: 'q' } },
  ],
};
