// =============================================================================
// PlainLanguageExplanation
//
// User-friendly summary of a governance decision. Shared between the Audit
// Certificates view (where it sits above the technical 11-layer dump) and
// the Governance Replay view (where it sits inline under each evaluation
// row).
//
// Accepts a record that may be either:
//   - a full Trust Certificate (from GET /v2/certificates/{id})
//   - a full GovernanceEvaluation (from GET /v2/evaluations/{id} or
//     GET /v2/artifacts/{id}/evaluations)
//   - a slim replay summary (from POST /v2/replay)
//
// The generator is tolerant of missing fields: richer input yields richer
// language; slim input still produces a useful paragraph.
//
// No backend calls, no mutation, no behavior change. Pure presentation
// over data the caller already has.
// =============================================================================

const DIMENSION_NAME = {
  B: 'Boundedness',
  A: 'Attribution',
  C: 'Compliance',
  K: 'Known',
};

const DECISION_VERB = {
  Allow:               'allowed',
  Observe:             'delivered with monitoring',
  Hold:                'held for human review',
  Escalate:            'escalated for senior review',
  Stop:                'blocked',
  Allow_with_logging:  'allowed (with enhanced logging)',
  Allow_with_redaction:'allowed (with redaction applied)',
  Allow_with_step_up:  'allowed (after step-up verification)',
  Rollback:            'rolled back',
};

const DECISION_TONE = {
  Allow:               'text-green-300',
  Observe:             'text-green-300',
  Hold:                'text-amber-300',
  Escalate:            'text-orange-300',
  Stop:                'text-red-300',
  Allow_with_logging:  'text-green-300',
  Allow_with_redaction:'text-green-300',
  Allow_with_step_up:  'text-amber-300',
  Rollback:            'text-red-300',
};

// Translate machine sub-factor labels to short human phrases. Returns the
// raw label if no translation is known — that keeps unfamiliar sub-factors
// surfaced rather than silently dropped.
const SUBFACTOR_PHRASE = {
  B1: 'request-time policy bounding',
  B2: 'authorization tier check',
  B3: 'request-time identity verification',
  A1: 'source attribution',
  A2: 'source quality',
  A3: 'integration-boundary attribution',
  C1: 'general compliance check',
  C2: 'standards-specific compliance',
  C3: 'prohibited-pattern detection',
  K1: 'calibration confidence',
  K2: 'evidence freshness',
  K3: 'chain calibration',
};

// Paraphrase the structured ``blocking_reason`` string into a short
// human phrase. Examples:
//
//   "C3_prohibited_pattern_prompt_injection_pattern:prohibited_prompt_injection_pattern"
//     -> "a prohibited prompt-injection pattern"
//   "C3_prohibited_pattern_credentials_pattern:prohibited_credentials_pattern"
//     -> "a prohibited credential-exposure pattern"
//   "gate_failure_K"
//     -> "a Known-dimension gate failure"
//   "context_expansion_invalidation"
//     -> "a context-expansion invalidation"
//
// Falls back to a softly-cleaned version of the raw string.
function paraphraseBlockingReason(raw) {
  if (!raw) return null;
  const s = String(raw);
  const lower = s.toLowerCase();
  if (lower.includes('prompt_injection')) return 'a prohibited prompt-injection pattern';
  if (lower.includes('credentials_pattern') || lower.includes('credential_exposure'))
    return 'a prohibited credential-exposure pattern';
  if (lower.includes('prohibited_action'))
    return 'a prohibited action pattern';
  if (lower.includes('context_expansion'))
    return 'a context-expansion invalidation (new evidence arrived after evaluation)';
  if (lower.startsWith('gate_failure_')) {
    const dim = lower.slice('gate_failure_'.length).toUpperCase();
    const name = DIMENSION_NAME[dim] || dim;
    return `a ${name}-dimension gate failure`;
  }
  // Generic cleanup: take the part after ':' if present, replace underscores.
  const last = s.includes(':') ? s.split(':').pop() : s;
  return last.replace(/_/g, ' ').toLowerCase();
}

// Pick a single "primary cause" string from whatever evidence the TC /
// evaluation carries. Priority order, highest first:
//   1. governance_rule_matches[*].effect.explanation (already human prose)
//   2. blocking_reason (paraphrased)
//   3. failing_dimension_subfactors (mapped to phrase)
//   4. identity_binding shortfall (verified=false or low confidence)
//   5. key_concerns[0] (Layer E)
//   6. null
function primaryCause(tc) {
  const ruleMatches = Array.isArray(tc.governance_rule_matches)
    ? tc.governance_rule_matches
    : Array.isArray(tc.rule_matches) ? tc.rule_matches : [];
  for (const m of ruleMatches) {
    const explanation = m?.effect?.explanation;
    if (typeof explanation === 'string' && explanation.trim()) {
      return explanation.trim();
    }
  }

  const para = paraphraseBlockingReason(tc.blocking_reason);
  if (para) return para;

  const failing = tc.failing_dimension_subfactors || {};
  const entries = Object.entries(failing);
  if (entries.length > 0) {
    const phrases = [];
    for (const [dim, subs] of entries) {
      const subKeys = subs && typeof subs === 'object' ? Object.keys(subs) : [];
      for (const key of subKeys) {
        const phrase = SUBFACTOR_PHRASE[key];
        if (phrase) {
          phrases.push(`weak ${phrase}`);
        } else if (key) {
          phrases.push(`${DIMENSION_NAME[dim] || dim} sub-factor ${key} below threshold`);
        }
      }
    }
    if (phrases.length > 0) {
      return phrases.slice(0, 2).join('; ');
    }
  }

  const ib = tc.identity_binding;
  if (ib) {
    if (ib.identity_verified === false) {
      return 'no verified identity for the requesting party';
    }
    if (typeof ib.identity_confidence === 'number' && ib.identity_confidence < 0.5) {
      return `low identity confidence (${ib.identity_confidence.toFixed(2)})`;
    }
  }

  if (Array.isArray(tc.key_concerns) && tc.key_concerns.length > 0) {
    return String(tc.key_concerns[0]);
  }
  return null;
}

// Find which BACK dimensions failed their gate, if any. Returns a list of
// {dim, score, threshold} where score/threshold may be null if the input
// shape doesn't carry them (slim replay summary case).
function failedGateDetails(tc) {
  const gateResults = tc.gate_results || {};
  const componentScores = tc.component_scores || {};
  // Thresholds may be top-level (TC) or under a policy snapshot
  // (evaluation row). Try both.
  const thresholds =
    tc.thresholds
    || tc.policy_profile_snapshot?.thresholds
    || {};

  const out = [];
  for (const [dim, result] of Object.entries(gateResults)) {
    if (result === 'fail') {
      out.push({
        dim,
        score:     typeof componentScores[dim] === 'number' ? componentScores[dim] : null,
        threshold: typeof thresholds[dim] === 'number'    ? thresholds[dim]    : null,
      });
    }
  }
  return out;
}

function fmtScore(n) {
  if (typeof n !== 'number' || Number.isNaN(n)) return '—';
  return n.toFixed(4);
}
function fmtThreshold(n) {
  if (typeof n !== 'number' || Number.isNaN(n)) return '—';
  return n.toFixed(2);
}

// Format a single failed gate phrase, with or without threshold context.
function failedGatePhrase({ dim, score, threshold }) {
  const name = DIMENSION_NAME[dim] || dim;
  if (score != null && threshold != null) {
    return `the ${name} (${dim}) gate failed — score ${fmtScore(score)}, required ≥ ${fmtThreshold(threshold)}`;
  }
  if (score != null) {
    return `the ${name} (${dim}) gate failed — score ${fmtScore(score)}`;
  }
  return `the ${name} (${dim}) gate failed`;
}

// Format an override event timestamp for display. Same compact form used
// elsewhere in the UI ("MM/DD HH:MM").
function shortOverrideTime(iso) {
  if (!iso) return '';
  const datePart = iso.slice(5, 10).replace('-', '/');
  const timePart = iso.slice(11, 16);
  return `${datePart} ${timePart}`;
}

// Top-level renderer. Returns a self-contained block that the caller
// drops into its layout.
//
// Props:
//   tc         — the TC or evaluation-like record to summarize.
//   overrides  — optional array of override events from
//                /v2/govern/decisions/{tc_id}/override-history. Each
//                entry: { override_decision, override_actor,
//                override_at, override_reason_text }. When present and
//                non-empty, a "Human override" block is appended to the
//                summary listing every event newest-first.
//   compact    — when true, render in a smaller footprint suitable for
//                inline use under a row (Replay view). Default false.
export default function PlainLanguageExplanation({ tc, overrides, compact = false }) {
  if (!tc) return null;

  const decision = tc.decision || 'Unknown';
  const verb = DECISION_VERB[decision] || decision;
  const tone = DECISION_TONE[decision] || 'text-gray-200';

  const failed = failedGateDetails(tc);
  const cause = primaryCause(tc);
  const sBase = typeof tc.s_base === 'number' ? tc.s_base : null;
  const tisCurrent = typeof tc.tis_current === 'number' ? tc.tis_current : null;
  // soft-hold ceiling (κ) is only present on full evaluations with policy
  // snapshots, not on slim summaries.
  const kappa = tc.policy_profile_snapshot?.soft_hold_ceiling;

  // ---- Sentence 1: the verdict ------------------------------------------ //
  const sentenceVerdict = (
    <>
      This response was{' '}
      <span className={`font-semibold ${tone}`}>{verb}</span>.
    </>
  );

  // ---- Sentence 2: gate-level detail ------------------------------------ //
  let sentenceDetail = null;
  if (failed.length > 0) {
    const joined = failed.map(failedGatePhrase).join('; ');
    // capitalize first character only
    const cap = joined.charAt(0).toUpperCase() + joined.slice(1);
    sentenceDetail = <>{cap}.</>;
  } else if (decision === 'Allow' || decision === 'Observe' || decision.startsWith('Allow_with_')) {
    sentenceDetail = (
      <>
        All governance gates passed
        {tisCurrent != null
          ? <> (TIS_current = <span className="font-mono">{fmtScore(tisCurrent)}</span>)</>
          : null}
        .
      </>
    );
  } else if (decision === 'Escalate') {
    sentenceDetail = (
      <>
        Gates passed but the overall trust score fell into the escalation band
        {tisCurrent != null
          ? <> (TIS_current = <span className="font-mono">{fmtScore(tisCurrent)}</span>)</>
          : null}
        .
      </>
    );
  }

  // ---- Sentence 3: primary cause ---------------------------------------- //
  let sentenceCause = null;
  if (cause) {
    sentenceCause = <>Primary reason: {cause}.</>;
  }

  // ---- Sentence 4: remediability-floor framing -------------------------- //
  let sentenceFloor = null;
  if (sBase != null && typeof kappa === 'number') {
    if (decision === 'Hold') {
      sentenceFloor = (
        <>
          Overall trust score{' '}
          (S_base = <span className="font-mono">{fmtScore(sBase)}</span>){' '}
          is above the remediability floor{' '}
          (κ = <span className="font-mono">{fmtThreshold(kappa)}</span>),{' '}
          so the response is paused rather than blocked.
        </>
      );
    } else if (decision === 'Stop' && failed.length > 0) {
      sentenceFloor = (
        <>
          Overall trust score{' '}
          (S_base = <span className="font-mono">{fmtScore(sBase)}</span>){' '}
          is below the remediability floor{' '}
          (κ = <span className="font-mono">{fmtThreshold(kappa)}</span>),{' '}
          so the system blocked delivery rather than escalating.
        </>
      );
    }
  }

  // ---- Sentence 5: next step -------------------------------------------- //
  let sentenceNext = null;
  if (decision === 'Hold') {
    sentenceNext = <>Next step: a reviewer can release or escalate this response via the Hold Queue.</>;
  } else if (decision === 'Escalate') {
    const roles = Array.isArray(tc.escalation_routed_to)
      ? tc.escalation_routed_to.filter(Boolean)
      : [];
    if (roles.length > 0) {
      sentenceNext = (
        <>
          Routed to: <span className="font-mono">{roles.join(', ')}</span>.
          A senior reviewer can approve, hold, or stop via the Escalation Queue.
        </>
      );
    } else {
      sentenceNext = <>A senior reviewer can approve, hold, or stop via the Escalation Queue.</>;
    }
  } else if (decision === 'Stop') {
    sentenceNext = <>The response will not be delivered.</>;
  } else if (decision === 'Allow') {
    sentenceNext = <>No human action required.</>;
  } else if (decision === 'Observe') {
    sentenceNext = <>Delivered as-is; decision recorded as evidence, no intervention applied.</>;
  }

  const textSize = compact ? 'text-xs' : 'text-sm';
  const containerPad = compact ? 'p-2' : 'p-3';
  const headerSize = compact ? 'text-[10px]' : 'text-[11px]';
  const blockClass = compact
    ? 'bg-gray-900/60 border border-gray-800 rounded'
    : 'bg-gray-900 border border-gray-700 rounded-lg';

  // ---- Override section (if any) --------------------------------------- //
  // Listed newest-first so the most recent decision-effecting event sits
  // at the top. Each line: actor + when + decision + reason. The block
  // is rendered as its own outlined sub-card so it visually separates
  // from the original decision narrative.
  const overrideList = Array.isArray(overrides) ? overrides.filter(Boolean) : [];

  return (
    <div className={`${blockClass} ${containerPad}`}>
      <div className={`${headerSize} uppercase tracking-wide text-gray-500 mb-1.5`}>
        Plain-language summary
      </div>
      <p className={`${textSize} text-gray-200 leading-relaxed`}>
        {sentenceVerdict}{' '}
        {sentenceDetail && <>{sentenceDetail}{' '}</>}
        {sentenceCause && <>{sentenceCause}{' '}</>}
        {sentenceFloor && <>{sentenceFloor}{' '}</>}
      </p>
      {sentenceNext && (
        <p className={`${textSize} text-gray-400 leading-relaxed mt-1.5 italic`}>
          {sentenceNext}
        </p>
      )}

      {overrideList.length > 0 && (
        <div className={`mt-3 border border-blue-800/60 bg-blue-900/15 rounded ${compact ? 'p-2' : 'p-3'}`}>
          <div className={`${headerSize} uppercase tracking-wide text-blue-300 mb-1.5`}>
            {overrideList.length === 1
              ? 'Human override applied'
              : `Human override history (${overrideList.length})`}
          </div>
          <div className="space-y-2">
            {overrideList.map((ev, i) => {
              const ovrTone = DECISION_TONE[ev.override_decision] || 'text-gray-200';
              return (
                <div key={`${ev.override_at || 'ovr'}-${i}`} className="border-l-2 border-blue-700 pl-2">
                  <p className={`${textSize} text-gray-200 leading-relaxed`}>
                    A reviewer changed this decision to{' '}
                    <span className={`font-semibold ${ovrTone}`}>
                      {ev.override_decision || 'an override decision'}
                    </span>
                    {ev.override_actor
                      ? <> — actor <span className="font-mono">{ev.override_actor}</span></>
                      : null}
                    {ev.override_at
                      ? <> at <span className="font-mono">{shortOverrideTime(ev.override_at)}</span></>
                      : null}
                    .
                  </p>
                  {ev.override_reason_text && (
                    <p className={`${textSize} text-gray-300 leading-relaxed mt-0.5`}>
                      Reason: <span className="italic">{ev.override_reason_text}</span>
                    </p>
                  )}
                </div>
              );
            })}
          </div>
          <p className={`${headerSize} text-gray-500 italic mt-2`}>
            The original Trust Certificate is preserved (append-only).
            Overrides are recorded as additive lifecycle events.
          </p>
        </div>
      )}
    </div>
  );
}
