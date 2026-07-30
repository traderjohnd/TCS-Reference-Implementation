import { describe, it, expect } from 'vitest';
import {
  FIXED_SCORE_4DP,
  FIXED_PARAM_4DP,
  GovernedDecimalError,
  buildGovernBody,
  certificateVersion,
  compareScore,
  displayGoverned,
  normalizeCertificate,
  parseFixed4,
  parseProtectedMetadataError,
  parseScore4,
  validateStreamRow,
} from '../governedDecimal';
import {
  ORDINARY_422_BODY,
  PROTECTED_METADATA_422_BODY,
  UNSUPPORTED_CERT,
  V1_CERT,
  V2_CERT,
  V2_CERT_MALFORMED,
} from '../../test/fixtures';

describe('FIXED_SCORE_4DP', () => {
  it.each(['0.0000', '0.9000', '0.9999', '1.0000'])('accepts %s', (v) => {
    expect(FIXED_SCORE_4DP.test(v)).toBe(true);
  });
  it.each(['0.9', '0.90', '1.0', '1.0001', '-0.0000', '.9000',
           '01.0000', '9E-1', '0.90000', ' 0.9000'])('rejects %s', (v) => {
    expect(FIXED_SCORE_4DP.test(v)).toBe(false);
  });
});

describe('parseScore4 — string-strip scaled integers', () => {
  it('parses canonical strings without binary float round trips', () => {
    expect(parseScore4('0.9000', 'f')).toBe(9000);
    expect(parseScore4('1.0000', 'f')).toBe(10000);
    expect(parseScore4('0.0000', 'f')).toBe(0);
    expect(parseScore4('0.0001', 'f')).toBe(1);
  });
  it('rejects "0.9" — the real string-strip failure mode (9, not 9000)', () => {
    expect(() => parseScore4('0.9', 'f')).toThrow(GovernedDecimalError);
  });
  it.each(['0.90000', '9E-1', '-0.0000', 0.9, null, undefined, '1.5'])(
    'rejects non-canonical %s', (v) => {
      expect(() => parseScore4(v, 'f')).toThrow(GovernedDecimalError);
    });
  it('never echoes the offending value in the error', () => {
    try {
      parseScore4('sk-secret-value', 'field_x');
    } catch (e) {
      expect(String(e.message)).not.toContain('sk-secret-value');
      expect(e.field).toBe('field_x');
    }
  });
});

describe('parseFixed4 — BigInt non-score parameters', () => {
  it('parses parameters that exceed 1', () => {
    expect(parseFixed4('20.0000', 'elapsed_hours')).toBe(200000n);
    expect(parseFixed4('0.0500', 'decay')).toBe(500n);
  });
  it.each(['20', '20.0', '-1.0000', '1e2'])('rejects %s', (v) => {
    expect(() => parseFixed4(v, 'p')).toThrow(GovernedDecimalError);
  });
  it('FIXED_PARAM_4DP accepts multi-integer-digit values', () => {
    expect(FIXED_PARAM_4DP.test('13863.0000')).toBe(true);
  });
});

describe('compareScore', () => {
  it('compares via scaled integers, not lexical or float paths', () => {
    expect(compareScore('0.9000', '0.9300')).toBe(-1);
    expect(compareScore('0.9300', '0.9300')).toBe(0);
    expect(compareScore('1.0000', '0.9999')).toBe(1);
  });
});

describe('displayGoverned', () => {
  it('renders v2 canonical strings verbatim — zero precision loss', () => {
    expect(displayGoverned('0.9000')).toBe('0.9000');
    expect(displayGoverned('0.899996')).toBe('0.899996');
  });
  it('formats v1 numbers for display', () => {
    expect(displayGoverned(0.8806)).toBe('0.8806');
    expect(displayGoverned(0.94, 2)).toBe('0.94');
  });
  it('renders missing values as an em dash, never zero or NaN', () => {
    expect(displayGoverned(null)).toBe('—');
    expect(displayGoverned(undefined)).toBe('—');
    expect(displayGoverned(NaN)).toBe('—');
  });
});

describe('certificateVersion — strict dispatch', () => {
  it('absence means v1', () => {
    expect(certificateVersion(V1_CERT)).toBe(1);
  });
  it('explicit 1 means v1', () => {
    expect(certificateVersion({ certificate_schema_version: 1 })).toBe(1);
  });
  it('2 means v2', () => {
    expect(certificateVersion(V2_CERT)).toBe(2);
  });
  it.each([3, 0, '2', 'v2'])('%s is unsupported — never best-effort', (v) => {
    expect(certificateVersion({ certificate_schema_version: v }))
      .toBe('unsupported');
  });
});

describe('normalizeCertificate', () => {
  it('valid v2 record: ok, integer gate_result', () => {
    const n = normalizeCertificate(V2_CERT);
    expect(n.version).toBe(2);
    expect(n.integrity.ok).toBe(true);
    expect(n.gateResult).toBe(0);
  });
  it('v1 record: gateResult derived from gate_passed', () => {
    const n = normalizeCertificate(V1_CERT);
    expect(n.version).toBe(1);
    expect(n.gateResult).toBe(1);
    expect(n.integrity.ok).toBe(true);
  });
  it('malformed v2: names the fields, no coercion into v1 path', () => {
    const n = normalizeCertificate(V2_CERT_MALFORMED);
    expect(n.version).toBe(2);            // NOT downgraded to v1
    expect(n.integrity.ok).toBe(false);
    expect(n.integrity.problems).toContain('s_base');
    expect(n.integrity.problems).toContain('gate_result');
    expect(n.gateResult).toBeNull();      // no legacy-alias fallback
  });
  it('v2 with numeric governed values is invalid (strings required)', () => {
    const n = normalizeCertificate({ ...V2_CERT, tis_current: 0.0 });
    expect(n.integrity.ok).toBe(false);
    expect(n.integrity.problems).toContain('tis_current');
  });
  it('unknown schema version is unsupported', () => {
    const n = normalizeCertificate(UNSUPPORTED_CERT);
    expect(n.version).toBe('unsupported');
    expect(n.integrity.ok).toBe(false);
  });
});

describe('validateStreamRow', () => {
  it('accepts v1 float rows and v2 decimal-string rows', () => {
    expect(validateStreamRow({ tis_current: 0.88 }).ok).toBe(true);
    expect(validateStreamRow({ tis_current: '0.8800' }).ok).toBe(true);
  });
  it('flags malformed values by field name', () => {
    const r = validateStreamRow({ tis_current: '8.8e-1' });
    expect(r.ok).toBe(false);
    expect(r.problems).toEqual(['tis_current']);
  });
});

describe('buildGovernBody — typed evaluation-typing fields', () => {
  it('places risk_tier / action_class / connection_type top-level', () => {
    const body = buildGovernBody({
      query: 'q', candidateAnswer: 'a',
      riskTier: 'r2', actionClass: 'a3', connectionType: 'CT-4',
      extraMetadata: { note: 'display' },
    });
    expect(body.risk_tier).toBe('r2');
    expect(body.action_class).toBe('a3');
    expect(body.connection_type).toBe('CT-4');
    expect(body.extra_metadata).toEqual({ note: 'display' });
  });
  it('omits unset typed fields (defaults preserved)', () => {
    const body = buildGovernBody({ query: 'q', candidateAnswer: 'a' });
    expect('risk_tier' in body).toBe(false);
    expect('action_class' in body).toBe(false);
    expect('connection_type' in body).toBe(false);
  });
  it('refuses typed fields smuggled inside extra metadata', () => {
    expect(() => buildGovernBody({
      query: 'q', candidateAnswer: 'a',
      extraMetadata: { risk_tier: 'r1' },
    })).toThrow(GovernedDecimalError);
  });
});

describe('parseProtectedMetadataError — real backend envelope', () => {
  it('parses the structured violation under detail', () => {
    const v = parseProtectedMetadataError(
      PROTECTED_METADATA_422_BODY.detail);
    expect(v).not.toBeNull();
    expect(v.rejectedKeys).toEqual(['nested.C_score', 'risk_tier']);
    expect(v.message).toContain('typed request fields');
  });
  it('returns null for an ordinary pydantic-array 422', () => {
    expect(parseProtectedMetadataError(ORDINARY_422_BODY.detail))
      .toBeNull();
  });
  it('returns null for strings and unrelated objects', () => {
    expect(parseProtectedMetadataError('Not found')).toBeNull();
    expect(parseProtectedMetadataError({ error: 'other' })).toBeNull();
  });
});
