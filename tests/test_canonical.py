"""
tests/test_canonical.py
=======================

Coverage for :mod:`tcs.canonical` — the tis-v2 numerical foundations.

The module is added in strict isolation as tis-v2 Commit 1. Nothing in
the codebase consumes it yet; Commit 2 (engine v2 path) is the first
wire-up. These tests therefore validate the module against itself and
against the VERIFIED cases stated in the tis-v2 brief.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext, setcontext

import pytest

from tcs.canonical import (
    AdjustmentApplied,
    CALCULATION_VERSION_V2,
    CertificateInvariantError,
    DECAY_ALGORITHM_VERSION,
    PARAMETER_QUANTUM,
    SCORE_PRECISION_POLICY,
    SCORE_QUANTUM,
    SCORE_ROUNDING,
    ScoreValidationError,
    TIS_DECIMAL_CONTEXT,
    UnsupportedCalculationVersion,
    UnsupportedCertificateSchemaVersion,
    canonical_nonnegative_parameter,
    canonical_score,
    compute_weighted_score,
    require_canonical_parameter,
    require_canonical_score,
    serialize_canonical_score,
    serialize_raw_decimal,
)


# --------------------------------------------------------------------------- #
# Constants and version identifiers                                            #
# --------------------------------------------------------------------------- #

class TestConstants:
    def test_version_identifiers_are_the_committed_strings(self):
        # These strings appear on every tis-v2 certificate. Changing
        # them silently would break every existing v2 replay in the
        # field. The identifiers are load-bearing — this test pins
        # them so a future accidental edit fails loudly.
        assert SCORE_PRECISION_POLICY == (
            "decimal-4dp-half-up-each-decision-stage-context28-v1"
        )
        assert DECAY_ALGORITHM_VERSION == (
            "decimal-exp-context28-half-even-then-4dp-half-up-v1"
        )
        assert CALCULATION_VERSION_V2 == "tis-v2"

    def test_quanta_are_4dp(self):
        assert SCORE_QUANTUM == Decimal("0.0001")
        assert PARAMETER_QUANTUM == Decimal("0.0001")

    def test_rounding_mode_is_half_up(self):
        assert SCORE_ROUNDING == ROUND_HALF_UP

    def test_pinned_context_has_the_documented_precision(self):
        assert TIS_DECIMAL_CONTEXT.prec == 28
        assert TIS_DECIMAL_CONTEXT.rounding == ROUND_HALF_UP


# --------------------------------------------------------------------------- #
# Exception hierarchy                                                          #
# --------------------------------------------------------------------------- #

class TestExceptions:
    def test_all_four_are_valueerror_subclasses(self):
        # Callers may catch ValueError as a broad safety net; ensure
        # every canonical exception participates in that hierarchy.
        for exc in (
            ScoreValidationError,
            CertificateInvariantError,
            UnsupportedCalculationVersion,
            UnsupportedCertificateSchemaVersion,
        ):
            assert issubclass(exc, ValueError)

    def test_score_validation_and_certificate_invariant_are_distinct(self):
        # Two classes with the same base but distinct semantics.
        # A caller catching one MUST NOT accidentally catch the other,
        # or the input-vs-invariant distinction collapses.
        assert not issubclass(CertificateInvariantError, ScoreValidationError)
        assert not issubclass(ScoreValidationError, CertificateInvariantError)


# --------------------------------------------------------------------------- #
# canonical_score — score domain input canonicalizer                           #
# --------------------------------------------------------------------------- #

class TestCanonicalScorePositive:
    def test_short_form_expands_to_4dp(self):
        assert canonical_score("0.9") == Decimal("0.9000")

    def test_half_up_rounds_up_at_boundary(self):
        # VERIFIED at threshold 0.9000 under ROUND_HALF_UP (per brief):
        #   0.899950 rounds UP to 0.9000 (the exact half)
        #   0.900050 rounds UP to 0.9001
        assert canonical_score("0.899950") == Decimal("0.9000")
        assert canonical_score("0.900050") == Decimal("0.9001")

    def test_half_up_does_not_round_down_below_boundary(self):
        assert canonical_score("0.899949") == Decimal("0.8999")

    def test_endpoints(self):
        assert canonical_score("0") == Decimal("0.0000")
        assert canonical_score("1") == Decimal("1.0000")
        assert canonical_score(Decimal("1.0000")) == Decimal("1.0000")

    def test_accepts_decimal_input_from_str_construction(self):
        # Decimal input path is common — canonicalizer must accept it.
        assert canonical_score(Decimal("0.9")) == Decimal("0.9000")

    def test_negative_zero_input_normalizes_to_positive_zero(self):
        # -0.0000 == 0 numerically, but serializes differently and
        # would produce a distinct hash payload for the same score.
        # Canonicalization normalizes it to a non-signed zero.
        result = canonical_score(Decimal("-0.0000"))
        assert result == Decimal("0.0000")
        assert not result.is_signed()

    def test_positive_zero_stays_positive_zero(self):
        result = canonical_score("0")
        assert result == Decimal("0.0000")
        assert not result.is_signed()


class TestCanonicalScoreRejections:
    def test_nan_rejected(self):
        with pytest.raises(ScoreValidationError):
            canonical_score(Decimal("NaN"))

    def test_positive_infinity_rejected(self):
        with pytest.raises(ScoreValidationError):
            canonical_score(Decimal("Infinity"))

    def test_negative_infinity_rejected(self):
        with pytest.raises(ScoreValidationError):
            canonical_score(Decimal("-Infinity"))

    def test_slightly_negative_rejected_not_rounded_into_range(self):
        # Critical: -0.00001 must be REJECTED (< 0), not rounded to
        # 0.0000. The range check runs BEFORE quantization.
        with pytest.raises(ScoreValidationError):
            canonical_score("-0.00001")

    def test_out_of_range_high_rejected(self):
        with pytest.raises(ScoreValidationError):
            canonical_score("1.00001")

    def test_unparseable_rejected(self):
        with pytest.raises(ScoreValidationError):
            canonical_score("not-a-number")

    def test_none_rejected(self):
        with pytest.raises(ScoreValidationError):
            canonical_score(None)


class TestDecimalFloatVsStrConstruction:
    """Establishes why the brief mandates ``Decimal(str(v))`` and never
    ``Decimal(float)``. Once binary contamination has been embedded in
    a Decimal, the canonicalizer sees only the resulting decimal value
    and will quantize it — the original lexical form cannot be
    recovered. This test does NOT claim the canonicalizer rejects
    Decimal(0.9); it only demonstrates the divergence at construction."""

    def test_float_and_str_construction_differ(self):
        from_float = Decimal(0.9)
        from_str = Decimal("0.9")
        assert from_float != from_str
        # from_float is a long non-terminating representation of the
        # nearest binary float to 0.9. from_str is exactly 0.9.
        assert len(str(from_float)) > 3


# --------------------------------------------------------------------------- #
# canonical_nonnegative_parameter — parameter domain                           #
# --------------------------------------------------------------------------- #

class TestCanonicalNonnegativeParameter:
    def test_accepts_values_above_one(self):
        # elapsed_hours routinely exceeds 1. Parameter domain permits it.
        assert canonical_nonnegative_parameter(
            "24.5", field_name="elapsed_hours",
        ) == Decimal("24.5000")

    def test_accepts_zero(self):
        assert canonical_nonnegative_parameter(
            "0", field_name="elapsed_hours",
        ) == Decimal("0.0000")

    def test_maximum_enforced_when_supplied(self):
        with pytest.raises(ScoreValidationError):
            canonical_nonnegative_parameter(
                "1.5",
                field_name="capped_field",
                maximum=Decimal("1.0000"),
            )

    def test_negative_rejected(self):
        with pytest.raises(ScoreValidationError):
            canonical_nonnegative_parameter(
                "-0.5", field_name="elapsed_hours",
            )

    def test_nan_rejected(self):
        with pytest.raises(ScoreValidationError):
            canonical_nonnegative_parameter(
                Decimal("NaN"), field_name="elapsed_hours",
            )

    def test_infinity_rejected(self):
        with pytest.raises(ScoreValidationError):
            canonical_nonnegative_parameter(
                Decimal("Infinity"), field_name="elapsed_hours",
            )

    def test_negative_zero_normalized(self):
        result = canonical_nonnegative_parameter(
            Decimal("-0.0000"), field_name="x",
        )
        assert result == Decimal("0.0000")
        assert not result.is_signed()


# --------------------------------------------------------------------------- #
# require_canonical_score — internal validator, non-repairing                  #
# --------------------------------------------------------------------------- #

class TestRequireCanonicalScoreAcceptance:
    def test_canonical_value_passes(self):
        require_canonical_score(Decimal("0.9000"), "component_scores.B")

    def test_endpoints_pass(self):
        require_canonical_score(Decimal("0.0000"), "x")
        require_canonical_score(Decimal("1.0000"), "x")


class TestRequireCanonicalScoreRejections:
    """The validator must raise CertificateInvariantError on ALL
    failures — including domain failures that the underlying
    canonicalizer would surface as ScoreValidationError. Translation
    is explicit per the validator contract."""

    def test_non_decimal_rejected(self):
        # Strings and floats are never canonical, regardless of value.
        with pytest.raises(CertificateInvariantError):
            require_canonical_score("0.9000", "x")
        with pytest.raises(CertificateInvariantError):
            require_canonical_score(0.9, "x")

    def test_wider_scale_rejected(self):
        # Numerically 0.9 but with a wider quantum — passes ==
        # comparison but fails same_quantum. Must reject.
        with pytest.raises(CertificateInvariantError):
            require_canonical_score(Decimal("0.90000"), "x")

    def test_scientific_notation_rejected(self):
        with pytest.raises(CertificateInvariantError):
            require_canonical_score(Decimal("9E-1"), "x")

    def test_negative_zero_rejected(self):
        with pytest.raises(CertificateInvariantError):
            require_canonical_score(Decimal("-0.0000"), "x")

    def test_nan_raises_certificate_invariant_not_score_validation(self):
        with pytest.raises(CertificateInvariantError):
            require_canonical_score(Decimal("NaN"), "x")
        # And explicitly not the input-side exception:
        try:
            require_canonical_score(Decimal("NaN"), "x")
        except ScoreValidationError:
            pytest.fail(
                "validator leaked ScoreValidationError — must translate to "
                "CertificateInvariantError"
            )
        except CertificateInvariantError:
            pass

    def test_positive_infinity_raises_certificate_invariant(self):
        with pytest.raises(CertificateInvariantError):
            require_canonical_score(Decimal("Infinity"), "x")

    def test_negative_infinity_raises_certificate_invariant(self):
        with pytest.raises(CertificateInvariantError):
            require_canonical_score(Decimal("-Infinity"), "x")

    def test_negative_value_raises_certificate_invariant(self):
        with pytest.raises(CertificateInvariantError):
            require_canonical_score(Decimal("-0.5000"), "x")

    def test_out_of_range_high_raises_certificate_invariant(self):
        with pytest.raises(CertificateInvariantError):
            require_canonical_score(Decimal("1.0001"), "x")


# --------------------------------------------------------------------------- #
# require_canonical_parameter                                                  #
# --------------------------------------------------------------------------- #

class TestRequireCanonicalParameter:
    def test_canonical_value_passes(self):
        require_canonical_parameter(Decimal("24.5000"), "elapsed_hours")

    def test_value_above_one_passes(self):
        # Unlike the score validator, the parameter validator permits
        # values > 1 (unless a maximum is supplied).
        require_canonical_parameter(Decimal("100.0000"), "elapsed_hours")

    def test_maximum_enforced_translates_to_invariant_error(self):
        with pytest.raises(CertificateInvariantError):
            require_canonical_parameter(
                Decimal("2.0000"), "capped",
                maximum=Decimal("1.0000"),
            )

    def test_non_decimal_rejected(self):
        with pytest.raises(CertificateInvariantError):
            require_canonical_parameter("24.5000", "elapsed_hours")

    def test_wider_scale_rejected(self):
        with pytest.raises(CertificateInvariantError):
            require_canonical_parameter(Decimal("24.50000"), "elapsed_hours")

    def test_negative_zero_rejected(self):
        with pytest.raises(CertificateInvariantError):
            require_canonical_parameter(Decimal("-0.0000"), "elapsed_hours")

    def test_nan_translated_to_certificate_invariant(self):
        with pytest.raises(CertificateInvariantError):
            require_canonical_parameter(Decimal("NaN"), "elapsed_hours")
        try:
            require_canonical_parameter(Decimal("NaN"), "elapsed_hours")
        except ScoreValidationError:
            pytest.fail(
                "validator leaked ScoreValidationError — must translate to "
                "CertificateInvariantError"
            )
        except CertificateInvariantError:
            pass

    def test_infinity_translated_to_certificate_invariant(self):
        with pytest.raises(CertificateInvariantError):
            require_canonical_parameter(Decimal("Infinity"), "elapsed_hours")
        with pytest.raises(CertificateInvariantError):
            require_canonical_parameter(Decimal("-Infinity"), "elapsed_hours")

    def test_negative_translated_to_certificate_invariant(self):
        with pytest.raises(CertificateInvariantError):
            require_canonical_parameter(Decimal("-1.5000"), "elapsed_hours")


# --------------------------------------------------------------------------- #
# serialize_canonical_score — validating serializer                            #
# --------------------------------------------------------------------------- #

class TestSerializeCanonicalScore:
    def test_round_trip(self):
        wire = serialize_canonical_score(Decimal("0.9000"))
        assert wire == "0.9000"
        assert Decimal(wire) == Decimal("0.9000")

    def test_endpoints_serialize_with_4dp(self):
        assert serialize_canonical_score(Decimal("0.0000")) == "0.0000"
        assert serialize_canonical_score(Decimal("1.0000")) == "1.0000"

    def test_refuses_non_canonical_input_rather_than_repairing(self):
        # This is the SAME-BUCKET tampering case: 0.899996 quantizes
        # to 0.9000 under the canonicalizer, but the SERIALIZER must
        # refuse rather than silently laundering the tampering back
        # into the wire form.
        with pytest.raises(CertificateInvariantError):
            serialize_canonical_score(Decimal("0.899996"))

    def test_refuses_negative_zero(self):
        with pytest.raises(CertificateInvariantError):
            serialize_canonical_score(Decimal("-0.0000"))

    def test_refuses_wider_scale(self):
        with pytest.raises(CertificateInvariantError):
            serialize_canonical_score(Decimal("0.90000"))


# --------------------------------------------------------------------------- #
# serialize_raw_decimal — variable scale, lossless                             #
# --------------------------------------------------------------------------- #

class TestSerializeRawDecimal:
    """The one field whose purpose is to preserve what quantization
    discards. Fixed notation, no exponent, trailing zeros stripped,
    negative zero normalized."""

    VERIFIED_CASES = [
        # (input Decimal, expected wire string)
        (Decimal("0.9400"),   "0.94"),
        (Decimal("0.899996"), "0.899996"),
        (Decimal("1.0000"),   "1"),
        (Decimal("0.10"),     "0.1"),
        (Decimal("1E-7"),     "0.0000001"),
        (Decimal("-0"),       "0"),
        (Decimal("0"),        "0"),
    ]

    @pytest.mark.parametrize("value,expected", VERIFIED_CASES)
    def test_verified_cases_from_brief(self, value, expected):
        assert serialize_raw_decimal(value) == expected

    @pytest.mark.parametrize("value,expected", VERIFIED_CASES)
    def test_round_trip_is_numerically_lossless(self, value, expected):
        # Numerical (not lexical) round-trip: the wire form parses back
        # to the same Decimal value even when the wire form differs
        # from the original (e.g. "0.9400" -> "0.94" -> Decimal("0.94"),
        # which is numerically equal to Decimal("0.9400")).
        assert Decimal(serialize_raw_decimal(value)) == value

    def test_no_exponent_notation_in_wire_form(self):
        # format(v, "f") suppresses exponent — even for very small values.
        assert "E" not in serialize_raw_decimal(Decimal("1E-7"))
        assert "e" not in serialize_raw_decimal(Decimal("1E-7"))

    def test_rejects_non_decimal(self):
        with pytest.raises(ScoreValidationError):
            serialize_raw_decimal("0.94")
        with pytest.raises(ScoreValidationError):
            serialize_raw_decimal(0.94)

    def test_rejects_nan(self):
        with pytest.raises(ScoreValidationError):
            serialize_raw_decimal(Decimal("NaN"))

    def test_rejects_infinity(self):
        with pytest.raises(ScoreValidationError):
            serialize_raw_decimal(Decimal("Infinity"))
        with pytest.raises(ScoreValidationError):
            serialize_raw_decimal(Decimal("-Infinity"))

    def test_rejects_out_of_range(self):
        with pytest.raises(ScoreValidationError):
            serialize_raw_decimal(Decimal("1.5"))
        with pytest.raises(ScoreValidationError):
            serialize_raw_decimal(Decimal("-0.5"))


# --------------------------------------------------------------------------- #
# compute_weighted_score — pinned aggregator                                   #
# --------------------------------------------------------------------------- #

class TestComputeWeightedScore:
    """The sole entry point for pinned weighted-sum computation.
    Must produce identical results regardless of ambient global
    context (the whole reason for context pinning)."""

    # Weights and scores hand-computed to give s_base = 0.8444 under
    # 4dp canonicalization. These are the numbers the brief uses to
    # demonstrate the ambient-context defect.
    WEIGHTS = {
        "B": Decimal("0.2500"),
        "A": Decimal("0.2500"),
        "C": Decimal("0.3000"),
        "K": Decimal("0.2000"),
    }
    SCORES = {
        "B": Decimal("0.9000"),
        "A": Decimal("0.8500"),
        "C": Decimal("0.9200"),
        "K": Decimal("0.7222"),
    }

    def test_produces_a_canonical_score(self):
        result = compute_weighted_score(self.SCORES, self.WEIGHTS)
        require_canonical_score(result, "s_base")

    def test_identical_result_across_ambient_contexts(self):
        # This is the load-bearing property. Under the previous
        # un-pinned code, an ambient prec=4 vs prec=28 changed the
        # weighted-sum result, producing a different certificate for
        # the same inputs. With pinning, every ambient setting agrees.
        results = set()
        for prec in (28, 12, 6):
            with localcontext(Context(prec=prec, rounding=ROUND_HALF_EVEN)):
                results.add(
                    compute_weighted_score(self.SCORES, self.WEIGHTS)
                )
        assert len(results) == 1, (
            f"ambient context leaked into pinned computation: "
            f"got {results!r}"
        )

    def test_result_is_reproducible_from_manual_arithmetic(self):
        # Independent sanity check: run the same math outside the
        # helper, inside our own pinned context, and confirm agreement.
        with localcontext(TIS_DECIMAL_CONTEXT):
            manual = sum(
                (self.WEIGHTS[k] * self.SCORES[k] for k in self.WEIGHTS),
                Decimal("0"),
            ).quantize(SCORE_QUANTUM, rounding=SCORE_ROUNDING)
        assert compute_weighted_score(self.SCORES, self.WEIGHTS) == manual


# --------------------------------------------------------------------------- #
# AdjustmentApplied — shared dataclass                                         #
# --------------------------------------------------------------------------- #

class TestAdjustmentApplied:
    def test_construction_holds_decimal_internally(self):
        adj = AdjustmentApplied(
            rule_id="TCS_SPEC_19_1",
            dimension="B",
            value_before=Decimal("0.9400"),
            value_after=Decimal("0.3000"),
            reason="identity_confidence_below_0_30",
        )
        assert isinstance(adj.value_before, Decimal)
        assert isinstance(adj.value_after, Decimal)

    def test_is_frozen(self):
        adj = AdjustmentApplied(
            rule_id="r", dimension="B",
            value_before=Decimal("0.9400"),
            value_after=Decimal("0.3000"),
            reason="x",
        )
        with pytest.raises(FrozenInstanceError):
            adj.rule_id = "other"

    def test_replace_preserves_decimal_typed_fields(self):
        original = AdjustmentApplied(
            rule_id="r", dimension="B",
            value_before=Decimal("0.9400"),
            value_after=Decimal("0.3000"),
            reason="x",
        )
        updated = replace(original, value_after=Decimal("0.0000"))
        assert isinstance(updated.value_after, Decimal)
        assert updated.value_after == Decimal("0.0000")
        assert updated.value_before == original.value_before
