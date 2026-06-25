// =============================================================================
// PlainLanguageExplanation
//
// A reviewer-friendly summary of a governance decision. Used by the Audit
// Certificates view (above the technical 11-layer dump), the LiveDecisions
// TC drawer, and the Governance Replay view (inline under each evaluation
// row).
//
// Goal: a non-technical reviewer should read three short paragraphs and
// understand (1) what the user asked for, (2) why the system decided what
// it decided, in domain language, and (3) what should happen next. The
// raw governance math is demoted to a small parenthetical so an auditor
// can still see the numbers without them dominating the page.
//
// Accepts a record that may be:
//   - a full Trust Certificate (from GET /v2/certificates/{id})
//   - a full GovernanceEvaluation (from GET /v2/evaluations/{id})
//   - a slim replay summary (from POST /v2/replay)
//
// The generator is tolerant of missing fields. No backend calls, no
// mutation, no LLM call — pure presentation over data the caller already
// has.
// =============================================================================

const DIMENSION_NAME = {
  B: 'Boundedness',
  A: 'Attribution',
  C: 'Compliance',
  K: 'Known',
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

// Map TC.domain to a reviewer role and a topic phrase used in the
// lead sentence. Unknown domains fall back to neutral language so the
// summary never looks broken.
const DOMAIN_PROFILE = {
  financial_services:    { reviewer: 'compliance officer',  topic: 'financial-advisory question' },
  investment_advisory:   { reviewer: 'compliance officer',  topic: 'financial-advisory question' },
  life_sciences:         { reviewer: 'clinical reviewer',    topic: 'clinical question' },
  healthcare:            { reviewer: 'clinical reviewer',    topic: 'clinical question' },
  medical_devices:       { reviewer: 'clinical reviewer',    topic: 'clinical-decision-support question' },
  clinical_cds:          { reviewer: 'clinical reviewer',    topic: 'clinical-decision-support question' },
  pharmacovigilance:     { reviewer: 'pharmacovigilance reviewer', topic: 'pharmacovigilance question' },
  pharma:                { reviewer: 'pharmacovigilance reviewer', topic: 'pharmacovigilance question' },
  enterprise_ops:        { reviewer: 'operations lead',      topic: 'operations question' },
  federal_public:        { reviewer: 'policy reviewer',      topic: 'policy question' },
  general_ai_governance: { reviewer: 'reviewer',             topic: 'request' },
  unknown:               { reviewer: 'reviewer',             topic: 'request' },
};

function profileFor(domain) {
  if (!domain) return DOMAIN_PROFILE.unknown;
  return DOMAIN_PROFILE[domain] || DOMAIN_PROFILE.unknown;
}

// What each gated dimension represents in domain language. Used to
// describe gate failures without leaking the dimension code (B/A/C/K)
// as the primary noun.
const DIMENSION_PLAIN = {
  B: { short: 'authorization',  long: "the requester's authorization for this kind of request" },
  A: { short: 'source backing', long: 'the evidence and source attribution behind the answer' },
  C: { short: 'policy compliance', long: 'the policy-compliance check on the question or answer' },
  K: { short: 'answer confidence', long: "the system's calibration confidence in the answer" },
};

// Trim a prompt to a short reference phrase suitable for a single
// sentence. Keeps full prompts up to ~70 chars verbatim; longer ones
// get a tail-trim ellipsis.
function shortPromptRef(prompt) {
  if (!prompt) return null;
  const trimmed = String(prompt).replace(/\s+/g, ' ').trim();
  if (!trimmed) return null;
  if (trimmed.length <= 70) return trimmed;
  return trimmed.slice(0, 67).replace(/[\s.,;:]+$/, '') + '…';
}

// Number formatters used in the optional supplementary parenthetical.
function fmtScore(n) {
  if (typeof n !== 'number' || Number.isNaN(n)) return '—';
  return n.toFixed(4);
}
function fmtThreshold(n) {
  if (typeof n !== 'number' || Number.isNaN(n)) return '—';
  return n.toFixed(2);
}

// Format an override event timestamp for display. Compact form
// matching the rest of the UI ("MM/DD HH:MM").
function shortOverrideTime(iso) {
  if (!iso) return '';
  const datePart = iso.slice(5, 10).replace('-', '/');
  const timePart = iso.slice(11, 16);
  return `${datePart} ${timePart}`;
}

// Find which BACK dimensions failed their gate, if any. Returns
// {dim, score, threshold} entries (score/threshold may be null when
// the input shape doesn't carry them, e.g. slim replay summary).
function failedGateDetails(tc) {
  const gateResults = tc.gate_results || {};
  const componentScores = tc.component_scores || {};
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

// Paraphrase a structured ``blocking_reason`` into plain prose. Used
// for Stop and Hold narratives where the engine emitted a marker
// string the UI should translate.
function paraphraseBlockingReason(raw) {
  if (!raw) return null;
  const lower = String(raw).toLowerCase();
  if (lower.includes('prompt_injection'))
    return 'the question contains a prohibited prompt-injection pattern — language that tries to get the model to ignore safety policy';
  if (lower.includes('credentials_pattern') || lower.includes('credential_exposure'))
    return 'the question or context appears to expose credentials, which the policy prohibits';
  if (lower.includes('prohibited_action'))
    return 'the request matches a prohibited-action pattern under the active policy';
  if (lower.includes('context_expansion'))
    return 'new context arrived after the answer was evaluated, invalidating the original decision';
  if (lower.startsWith('query_off_topic_to_active_pack'))
    return "the question doesn't relate to the content the active policy pack governs — the system couldn't find sufficiently relevant sources in the active corpus to ground an answer, so the LLM was not invoked";
  // Generic cleanup as a last resort.
  return null;
}

// Detect the structured off-topic marker so we can short-circuit the
// usual gate-failure narrative and lead with "this is out of scope"
// framing instead. Returns null when the TC isn't off-topic.
function offTopicInfo(tc) {
  const raw = tc?.blocking_reason;
  if (typeof raw !== 'string') return null;
  if (!raw.startsWith('query_off_topic_to_active_pack')) return null;
  // marker shape:
  //   query_off_topic_to_active_pack:max_similarity=0.1234_threshold=0.50
  const simMatch = raw.match(/max_similarity=([0-9.]+)/);
  const thrMatch = raw.match(/threshold=([0-9.]+)/);
  return {
    maxSimilarity: simMatch ? parseFloat(simMatch[1]) : null,
    threshold:     thrMatch ? parseFloat(thrMatch[1]) : null,
  };
}

// Look for a rule-supplied human explanation. Typed-context and
// term-group rules carry an ``effect.explanation`` string that is
// already plain language ("lithium is contraindicated in pregnancy…");
// use it verbatim when present.
function ruleExplanation(tc) {
  const matches = Array.isArray(tc.governance_rule_matches)
    ? tc.governance_rule_matches
    : Array.isArray(tc.rule_matches) ? tc.rule_matches : [];
  for (const m of matches) {
    const explanation = m?.effect?.explanation;
    if (typeof explanation === 'string' && explanation.trim()) {
      return explanation.trim();
    }
  }
  return null;
}

// =============================================================================
// The renderer
// =============================================================================

export default function PlainLanguageExplanation({
  tc,
  prompt,           // optional — the original prompt/question. When
                    // present, the summary's lead sentence references
                    // it ("To answer 'lithium dosing…', the system…").
  overrides,        // optional — events from
                    // /v2/govern/decisions/{tc_id}/override-history.
  compact = false,  // smaller footprint for inline use under a row.
}) {
  if (!tc) return null;

  const decision = tc.decision || 'Unknown';
  const tone = DECISION_TONE[decision] || 'text-gray-200';
  const domain = tc.domain || tc.policy_profile_snapshot?.domain || 'unknown';
  const profile = profileFor(domain);
  const promptRef = shortPromptRef(prompt);

  const failed = failedGateDetails(tc);
  const ruleSays = ruleExplanation(tc);
  const sBase = typeof tc.s_base === 'number' ? tc.s_base : null;
  const kappa = tc.policy_profile_snapshot?.soft_hold_ceiling;
  const offTopic = offTopicInfo(tc);

  // ---- Lead sentence: "what happened" + reference to the prompt --------- //
  // For Allow/Observe we frame positively ("This {topic} was delivered.").
  // For Hold/Escalate/Stop we frame as "What's needed" ("To deliver this
  // {topic}, the system needs …").

  let leadJsx = null;
  const verbSpan = (txt) => <span className={`font-semibold ${tone}`}>{txt}</span>;

  if (decision === 'Allow') {
    leadJsx = (
      <>
        This {profile.topic} was {verbSpan('answered and delivered')}.
        {promptRef ? <> The original question — “<span className="italic">{promptRef}</span>” — passed every automated governance check.</> : <> Every automated governance check passed.</>}
      </>
    );
  } else if (decision === 'Observe') {
    leadJsx = (
      <>
        This {profile.topic} was {verbSpan('delivered with monitoring')}. The decision was recorded as evidence; no human intervention applied.
      </>
    );
  } else if (decision === 'Hold' && offTopic) {
    leadJsx = promptRef ? (
      <>
        The question{' '}
        “<span className="italic">{promptRef}</span>”{' '}
        is {verbSpan('out of scope')} for the active policy pack. The system held the request before invoking the LLM.
      </>
    ) : (
      <>
        This request is {verbSpan('out of scope')} for the active policy pack. The system held it before invoking the LLM.
      </>
    );
  } else if (decision === 'Hold') {
    leadJsx = promptRef ? (
      <>
        Before the answer to{' '}
        “<span className="italic">{promptRef}</span>”{' '}
        can be delivered, the system needs a {profile.reviewer} to verify it ({verbSpan('held for review')}).
      </>
    ) : (
      <>
        Before this {profile.topic} can be delivered, the system needs a {profile.reviewer} to verify the answer ({verbSpan('held for review')}).
      </>
    );
  } else if (decision === 'Escalate') {
    const roles = Array.isArray(tc.escalation_routed_to) ? tc.escalation_routed_to.filter(Boolean) : [];
    const routed = roles.length ? <> Routed to: <span className="font-mono">{roles.join(', ')}</span>.</> : null;
    leadJsx = promptRef ? (
      <>
        The answer to{' '}
        “<span className="italic">{promptRef}</span>”{' '}
        needs senior review before it can be released ({verbSpan('escalated')}).{routed}
      </>
    ) : (
      <>
        This {profile.topic} needs senior review before it can be released ({verbSpan('escalated')}).{routed}
      </>
    );
  } else if (decision === 'Stop') {
    leadJsx = promptRef ? (
      <>
        The request{' '}
        “<span className="italic">{promptRef}</span>”{' '}
        was {verbSpan('blocked')}. The response will not be delivered.
      </>
    ) : (
      <>
        This request was {verbSpan('blocked')}. The response will not be delivered.
      </>
    );
  } else {
    // Phase 3 refinements (Allow_with_*, Rollback, etc.).
    leadJsx = (
      <>
        This {profile.topic} resulted in <span className={`font-semibold ${tone}`}>{decision}</span>.
      </>
    );
  }

  // ---- Why sentence: plain prose, never raw machine strings ------------ //
  // Priority order:
  //   1) Rule-supplied explanation (already human prose)
  //   2) Paraphrased blocking_reason (for Stop with C3 patterns)
  //   3) Generated from failing gate + domain context
  //   4) Generic fallback

  let whyJsx = null;
  let supplementJsx = null;

  if (offTopic && decision === 'Hold') {
    // Off-topic gets its own narrative — leads with "out of scope"
    // and explains the system did not call the LLM, instead of the
    // generic A-gate-failure framing.
    whyJsx = (
      <>
        The reason: the question doesn't relate to the content the active
        policy pack governs. Retrieved sources from the active corpus were
        weak matches, so the system held the request rather than invoking
        the LLM on content it isn't prepared to ground.
      </>
    );
    if (offTopic.maxSimilarity != null && offTopic.threshold != null) {
      supplementJsx = (
        <span className="text-gray-500">
          {' '}(best retrieval match: similarity{' '}
          {offTopic.maxSimilarity.toFixed(3)}; off-topic threshold{' '}
          {offTopic.threshold.toFixed(2)})
        </span>
      );
    }
  } else if (ruleSays && (decision === 'Stop' || decision === 'Hold' || decision === 'Escalate')) {
    whyJsx = <>The reason: {ruleSays}</>;
  } else if (decision === 'Stop') {
    const paraphrased = paraphraseBlockingReason(tc.blocking_reason);
    if (paraphrased) {
      whyJsx = <>The reason: {paraphrased}.</>;
    } else if (failed.length > 0) {
      const dim = failed[0].dim;
      const plain = DIMENSION_PLAIN[dim];
      whyJsx = plain ? (
        <>The reason: the automated check on {plain.long} did not pass for this {profile.topic}, and the overall trust score sits below the threshold where the system is willing to escalate rather than block.</>
      ) : <>The reason: the {DIMENSION_NAME[dim] || dim} check did not pass and the overall trust score fell below the remediability floor.</>;
    } else {
      whyJsx = <>The reason: the request did not pass automated governance.</>;
    }
  } else if ((decision === 'Hold' || decision === 'Escalate') && failed.length > 0) {
    // Build a domain-flavored explanation per failed dimension.
    const phrases = failed.map(({ dim }) => {
      const plain = DIMENSION_PLAIN[dim];
      if (!plain) return `the ${DIMENSION_NAME[dim] || dim} check did not pass`;
      if (dim === 'K') {
        return `the system's confidence in the answer didn't reach the bar required for ${profile.topic}s of this risk level — the information may be correct, the system simply isn't sure enough on its own`;
      }
      if (dim === 'A') {
        return 'the answer was not sufficiently backed by attributable sources';
      }
      if (dim === 'B') {
        return `the system could not confirm that the requester has the authorization required for this kind of ${profile.topic}`;
      }
      if (dim === 'C') {
        return 'the question or answer is bumping against a policy-compliance boundary that requires human judgment';
      }
      return `the automated check on ${plain.long} did not pass`;
    });
    whyJsx = <>The reason: {phrases.join('; ')}.</>;

    // Optional small parenthetical with the raw numbers — kept short and
    // visually demoted so it doesn't compete with the prose.
    const numbered = failed.filter(f => f.score != null && f.threshold != null);
    if (numbered.length > 0) {
      supplementJsx = (
        <span className="text-gray-500">
          {' '}({numbered.map(f =>
            `${f.dim} = ${fmtScore(f.score)}, required ≥ ${fmtThreshold(f.threshold)}`
          ).join('; ')}
          {decision === 'Hold' && sBase != null && typeof kappa === 'number'
            ? `; overall S_base = ${fmtScore(sBase)}, above floor κ = ${fmtThreshold(kappa)}`
            : ''}
          )
        </span>
      );
    }
  } else if (decision === 'Escalate') {
    whyJsx = <>The reason: every gate passed, but the combined trust score fell into the band where a more senior reviewer should sign off before delivery.</>;
  }

  // ---- Next-step sentence: domain-friendly action --------------------- //
  let nextJsx = null;
  if (offTopic && decision === 'Hold') {
    nextJsx = <>Next step: switch to a policy pack whose corpus covers this topic, ask a question that fits the current pack, or let a {profile.reviewer} review and (where appropriate) approve the off-topic request via the Hold Queue.</>;
  } else if (decision === 'Hold') {
    nextJsx = <>Next step: a {profile.reviewer} can release the response if it looks accurate, or escalate it if it needs revision. The action lives in the Hold Queue on the Live page.</>;
  } else if (decision === 'Escalate') {
    nextJsx = <>Next step: a senior {profile.reviewer} can approve, hold, or stop the response from the Escalation Queue on the Live page.</>;
  } else if (decision === 'Stop') {
    nextJsx = <>Next step: nothing automatic. If the block looks like a mistake, a {profile.reviewer} can review the certificate and (where the rule allows it) record a documented override.</>;
  } else if (decision === 'Allow') {
    nextJsx = <>Next step: none. The response is being delivered as part of normal operation.</>;
  } else if (decision === 'Observe') {
    nextJsx = <>Next step: none. The decision is logged as evidence for monitoring without changing delivery.</>;
  }

  // ---- Override block (unchanged from prior pass) ---------------------- //
  const overrideList = Array.isArray(overrides) ? overrides.filter(Boolean) : [];

  const textSize = compact ? 'text-xs' : 'text-sm';
  const containerPad = compact ? 'p-2' : 'p-3';
  const headerSize = compact ? 'text-[10px]' : 'text-[11px]';
  const blockClass = compact
    ? 'bg-gray-900/60 border border-gray-800 rounded'
    : 'bg-gray-900 border border-gray-700 rounded-lg';

  return (
    <div className={`${blockClass} ${containerPad}`}>
      <div className={`${headerSize} uppercase tracking-wide text-gray-500 mb-1.5`}>
        Plain-language summary
      </div>
      <p className={`${textSize} text-gray-200 leading-relaxed`}>
        {leadJsx}
      </p>
      {whyJsx && (
        <p className={`${textSize} text-gray-200 leading-relaxed mt-2`}>
          {whyJsx}
          {supplementJsx}
        </p>
      )}
      {nextJsx && (
        <p className={`${textSize} text-gray-400 leading-relaxed mt-2 italic`}>
          {nextJsx}
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
