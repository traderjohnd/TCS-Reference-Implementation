# Patient Identity, Privacy, and Authorization (2026)

## Patient Identity Verification

Every clinical AI recommendation must be tied to a verified patient
identity. Patient identity is verified when:

1. The patient ID (MRN) has been confirmed against the EHR record.
2. Two patient identifiers (name + DOB, or MRN + DOB) have been
   cross-checked.
3. The requesting clinician's identity has been authenticated and
   their role authorizes the requested data access.

**AI systems must not provide patient-specific recommendations to
unauthenticated requestors or to requestors whose role does not
authorize access to the requested sensitivity tier.**

## Sensitivity Tiers

Patient data is classified into four tiers:

- **T0** — Public health information (general guidelines, drug labels)
- **T1** — De-identified or aggregate data
- **T2** — Identifiable patient data (medications, problem list, results)
- **T3** — Highly sensitive data (mental health, genetic, substance use,
  HIV status, reproductive health)

T2 and T3 access requires identity verification at the matching
authorization tier.

## Identity Failure Modes

When identity verification fails or is incomplete, the AI system must:

1. **Refuse to disclose patient-specific data** for that patient context.
2. **Provide general (T0) information only** if requested.
3. **Log the identity-binding failure** to the governance trail.
4. **Hold the response for human review** if the requestor is partially
   authenticated but the requested data tier exceeds their role.

## Common Identity Verification Gaps

These scenarios trigger an identity Hold:

- "What medications is patient John Doe currently taking?" (name alone is
  insufficient identification; multiple patients may share a name)
- "Show me the chart for the 65-year-old female in bed 12" (room number
  and demographics are not authoritative identifiers)
- "List active conditions for patient ID 12345" (MRN alone without
  second identifier and without clinician role check)

## HIPAA and Regulatory Alignment

This protocol implements the Minimum Necessary standard under HIPAA
§164.502(b) and supports SaMD identity requirements under FDA 21 CFR
Part 11 §11.10 (Controls for closed systems). Under the active TCS
policy, these protocols are interpreted as elevated Attribution and
Boundedness requirements.
