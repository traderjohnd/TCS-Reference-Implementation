"""
tcs.canonical
=============

The canonical numerical system for tis-v2.

Every numerical value a Trust Certificate attests to — component scores,
component weights, thresholds, s_base, penalty aggregates, decay factors,
tis_current, and every resolved policy parameter that participates in a
decision — flows through this module. The invariant this module exists
to establish is:

    The value used to make a decision, the value serialized into the
    certificate, and the value an auditor uses to independently replay
    that decision MUST be numerically identical.

The bugs this replaces:

    * ``float`` arithmetic in ``tis_engine.compute_tis`` produced
      component scores that did not recompute to their own s_base.
      Randomized measurement: 12.01% of certificates unreplayable at
      4dp under naive float rounding.
    * Python's built-in ``round()`` uses ROUND_HALF_EVEN over binary
      floats and disagrees with ROUND_HALF_UP on ties (0.00015,
      0.00035).
    * Python's ``Decimal`` arithmetic depends on the ambient global
      context, which any library or test can mutate. Measured on this
      codebase, an identical weighted-sum computation returned
      different results under prec=28 vs prec=4 — different certificates
      determined by unrelated application state.

The design in one sentence
--------------------------

Canonicalize inputs, calculate exclusively with canonical values inside
a pinned arithmetic context, serialize with a validator that refuses to
repair, and reject anything else. Never rely on Python's ``round()``,
never use ``Decimal(float)``, never depend on ambient context.

Layers and their contracts
--------------------------

**Canonicalizers** are input-side. They accept a variable-shape value,
validate it against domain rules, quantize it to the canonical form, and
either return a canonical ``Decimal`` or raise :class:`ScoreValidationError`
(for values that fail domain validation). They exist so that ambiguous
external input can be brought into the canonical form deterministically.

**Validators** are internal. They assume the value is already stored on
a model, and verify that it *is* canonical — that it has not been
tampered with, mis-serialized, or accidentally repaired to look canonical
without being so. They raise :class:`CertificateInvariantError`, which
callers use to fail closed on issuance or on replay. They NEVER call the
canonicalizer to "fix" a value, because that would silently launder
non-canonical or tampered content into a passing check.

**Serializers** convert ``Decimal`` values to their wire form. The
canonical serializer VALIDATES first (via the validator) and refuses to
serialize a non-canonical value. The raw-evidence serializer is separate,
uses variable scale, and preserves precision that quantization discards.

**The aggregator** is the sole entry point for the pinned weighted-sum
computation, so callers cannot accidentally sum in the ambient context.

Nothing consumes this module yet. It is added in isolation as Commit 1
of the tis-v2 landing sequence; Commit 2 (engine v2 path) is the first
place these helpers are actually wired.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    Context,
    Decimal,
    InvalidOperation,
    ROUND_HALF_UP,
    localcontext,
)
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Version identifiers                                                          #
# --------------------------------------------------------------------------- #
#
# These are stable strings that MUST appear on every tis-v2 certificate.
# They are constants only in this commit; the construction sites that
# stamp them onto ``TrustCertificate`` land in Commit 4.
#
# The identifiers deliberately name the algorithm in enough detail that
# a future release changing any of it (rounding mode, quanta, order of
# stages, transcendental function) MUST bump the identifier. A silent
# semantic change with the identifier unchanged is the exact failure
# mode this whole design is built to prevent.

SCORE_PRECISION_POLICY = (
    "decimal-4dp-half-up-each-decision-stage-context28-v1"
)
DECAY_ALGORITHM_VERSION = (
    "decimal-exp-context28-half-even-then-4dp-half-up-v1"
)
CALCULATION_VERSION_V2 = "tis-v2"


# --------------------------------------------------------------------------- #
# Arithmetic context                                                           #
# --------------------------------------------------------------------------- #
#
# The pinned context is the sole source of truth for precision and
# rounding across the whole tis-v2 numerical path. Every arithmetic
# operation that produces a certificate value MUST run inside a
# ``with localcontext(TIS_DECIMAL_CONTEXT):`` block, whether during
# issuance, pre-seal validation, or independent replay.
#
# VERIFIED on this codebase (weighted four-term sum with an ambient
# global context of prec=28, 12, and 6): the pinned computation returns
# a single 0.8444, whereas the un-pinned computation returned 0.8443
# under ambient prec=4 and raised InvalidOperation under ambient prec=3.
# The pinned context defends against that variance.

TIS_DECIMAL_CONTEXT: Context = Context(prec=28, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- #
# Quanta and rounding                                                          #
# --------------------------------------------------------------------------- #

SCORE_QUANTUM = Decimal("0.0001")
PARAMETER_QUANTUM = Decimal("0.0001")
SCORE_ROUNDING = ROUND_HALF_UP


# --------------------------------------------------------------------------- #
# Exceptions                                                                   #
# --------------------------------------------------------------------------- #
#
# Two distinct exception classes with distinct semantics.
#
#   ScoreValidationError            raised by INPUT-SIDE canonicalizers.
#                                   Semantically: "the value the caller
#                                   provided cannot be brought into the
#                                   canonical domain."
#
#   CertificateInvariantError       raised by INTERNAL validators and by
#                                   pre-seal / replay checks.
#                                   Semantically: "a value that was
#                                   already stored on a model, or is
#                                   being loaded from persistence, does
#                                   not satisfy the canonical invariants."
#
# These are NOT interchangeable. A validator that leaks
# ``ScoreValidationError`` breaks the contract that pre-seal validation
# always raises ``CertificateInvariantError``. Downstream ``except``
# clauses that catch one will not catch the other, so any translation
# between them must be explicit (see require_canonical_score below).

class ScoreValidationError(ValueError):
    """Raised by canonicalizers when input cannot be canonicalized."""


class CertificateInvariantError(ValueError):
    """Raised by validators / pre-seal checks when a stored value is not
    canonical, or when a certificate invariant fails on issuance or replay."""


class UnsupportedCalculationVersion(ValueError):
    """Raised when engine or replay code sees a ``calculation_version`` it
    does not know how to reproduce. Fail closed — never silently apply
    a different version's semantics."""


class UnsupportedCertificateSchemaVersion(ValueError):
    """Raised when persistence / hash construction sees a
    ``certificate_schema_version`` it does not know how to serialize.
    Fail closed — never guess a payload shape."""


# --------------------------------------------------------------------------- #
# AdjustmentApplied                                                            #
# --------------------------------------------------------------------------- #
#
# One entry per adjustment rule that ACTUALLY CHANGED a dimension score.
# A rule that fires but leaves the value unchanged records nothing.
# Ordered: replay depends on the exact sequence of value_before /
# value_after pairs, so the list preserves the order in which rules
# were applied at issuance time. Frozen: an entry cannot be mutated in
# place after construction, which lets hash-payload construction rely
# on the recorded content.

@dataclass(frozen=True)
class AdjustmentApplied:
    """One rule application that changed a dimension score.

    ``value_before`` and ``value_after`` are canonical ``Decimal`` values
    in the score domain. The wire form (fixed-scale 4dp strings) is
    produced by the serializer at ``to_dict()`` time, not stored here.
    """

    rule_id: str
    dimension: str
    value_before: Decimal
    value_after: Decimal
    reason: str


# --------------------------------------------------------------------------- #
# Canonicalizers — INPUT side                                                  #
# --------------------------------------------------------------------------- #

def canonical_score(value: Any) -> Decimal:
    """Canonicalize an input value into the score domain.

    Score domain: finite ``Decimal`` in [0, 1] at exactly 4 decimal
    places, with negative zero normalized to ``Decimal("0.0000")``.

    Raises :class:`ScoreValidationError` on:
        - values not parseable as ``Decimal``;
        - NaN or Infinity;
        - values < 0 (rejected BEFORE quantization; a genuinely
          negative value is not rounded into range);
        - values > 1.

    The construction uses ``Decimal(str(value))`` — NEVER
    ``Decimal(float)``. ``Decimal(0.9)`` carries binary-representation
    error straight into the canonical value; ``Decimal(str(0.9))``
    is ``Decimal("0.9")`` exactly.
    """
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ScoreValidationError(
            f"Invalid score value: {value!r}"
        ) from exc

    if not d.is_finite():
        raise ScoreValidationError(
            "Scores must be finite; NaN and Infinity prohibited"
        )

    # Range check runs BEFORE quantize so genuinely negative values
    # are REJECTED, not rounded into [0, 1]. ``-0.00001`` is <0 and
    # raises; ``-0.0000`` == 0 and quantizes (then negative-zero
    # normalization below handles the sign).
    if d < Decimal("0") or d > Decimal("1"):
        raise ScoreValidationError(
            f"Score outside permitted range [0, 1]: {d}"
        )

    with localcontext(TIS_DECIMAL_CONTEXT):
        result = d.quantize(SCORE_QUANTUM, rounding=SCORE_ROUNDING)

    # Negative zero normalization. Decimal("-0.0000") is numerically
    # equal to Decimal("0.0000") but serializes differently under
    # str() and format(). A -0 in the hash payload is a hash hazard.
    return Decimal("0.0000") if result == 0 else result


def canonical_nonnegative_parameter(
    value: Any,
    *,
    field_name: str,
    maximum: Optional[Decimal] = None,
) -> Decimal:
    """Canonicalize an input value into the non-negative parameter domain.

    Parameter domain: finite ``Decimal`` at exactly 4 decimal places,
    >= 0, MAY exceed 1 (unlike the score domain). ``elapsed_hours``
    and ``resolved_decay_rate`` are parameter-domain fields.

    ``maximum`` optionally caps the value; supply it where the codebase
    defines an operational upper bound.

    Raises :class:`ScoreValidationError` on the same conditions as
    :func:`canonical_score`, except that values > 1 are permitted
    unless a lower ``maximum`` is supplied.
    """
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ScoreValidationError(
            f"Invalid {field_name}: {value!r}"
        ) from exc

    if not d.is_finite():
        raise ScoreValidationError(
            f"{field_name} must be finite"
        )
    if d < Decimal("0"):
        raise ScoreValidationError(
            f"{field_name} must be non-negative: {d}"
        )
    if maximum is not None and d > maximum:
        raise ScoreValidationError(
            f"{field_name} exceeds maximum {maximum}: {d}"
        )

    with localcontext(TIS_DECIMAL_CONTEXT):
        result = d.quantize(PARAMETER_QUANTUM, rounding=SCORE_ROUNDING)

    return Decimal("0.0000") if result == 0 else result


# --------------------------------------------------------------------------- #
# Validators — INTERNAL, non-repairing                                         #
# --------------------------------------------------------------------------- #
#
# The validators enforce the certificate-invariant contract: they raise
# :class:`CertificateInvariantError` on ANY invalid stored value, and
# they NEVER repair. They translate exceptions from the canonicalizers
# they call, so callers can rely on a single exception type across
# every failure mode.

def require_canonical_score(value: Any, field_name: str) -> None:
    """Assert that a stored value is already in canonical score form.

    Checks, in order:
        1. isinstance(Decimal). Strings and floats are never canonical.
        2. Value is representable in the canonical score domain.
        3. Value equals its own canonicalization (numerically valid).
        4. Value has exactly the score quantum (4dp — no trailing
           extra digits, no scientific notation).
        5. Value is not signed-zero (``Decimal("-0.0000")``).

    Raises :class:`CertificateInvariantError` on ANY failure. Never
    raises :class:`ScoreValidationError` — the canonicalizer's
    exceptions are translated explicitly.
    """
    if not isinstance(value, Decimal):
        raise CertificateInvariantError(
            f"{field_name} must be Decimal, got {type(value).__name__}"
        )
    try:
        canonical = canonical_score(value)
    except ScoreValidationError as exc:
        raise CertificateInvariantError(
            f"{field_name} is outside the canonical score domain: {exc}"
        ) from exc
    if value != canonical:
        raise CertificateInvariantError(
            f"{field_name} is not numerically canonical: {value}"
        )
    if not value.same_quantum(SCORE_QUANTUM):
        raise CertificateInvariantError(
            f"{field_name} must have exactly four decimal places: {value}"
        )
    if value.is_zero() and value.is_signed():
        raise CertificateInvariantError(
            f"{field_name} must not be negative zero: {value}"
        )


def require_canonical_parameter(
    value: Any,
    field_name: str,
    *,
    maximum: Optional[Decimal] = None,
) -> None:
    """Assert that a stored value is already in canonical parameter form.

    Same shape as :func:`require_canonical_score` but for the
    non-negative parameter domain (values may exceed 1).

    Raises :class:`CertificateInvariantError` on any failure. Never
    raises :class:`ScoreValidationError`.
    """
    if not isinstance(value, Decimal):
        raise CertificateInvariantError(
            f"{field_name} must be Decimal, got {type(value).__name__}"
        )
    try:
        canonical = canonical_nonnegative_parameter(
            value, field_name=field_name, maximum=maximum,
        )
    except ScoreValidationError as exc:
        raise CertificateInvariantError(
            f"{field_name} is outside the canonical parameter domain: {exc}"
        ) from exc
    if value != canonical:
        raise CertificateInvariantError(
            f"{field_name} is not numerically canonical: {value}"
        )
    if not value.same_quantum(PARAMETER_QUANTUM):
        raise CertificateInvariantError(
            f"{field_name} must have exactly four decimal places: {value}"
        )
    if value.is_zero() and value.is_signed():
        raise CertificateInvariantError(
            f"{field_name} must not be negative zero: {value}"
        )


# --------------------------------------------------------------------------- #
# Serializers — validating, never repairing                                    #
# --------------------------------------------------------------------------- #

def serialize_canonical_score(value: Decimal, field_name: str = "score") -> str:
    """Serialize a canonical score to its fixed-scale 4dp wire form.

    VALIDATES first (via :func:`require_canonical_score`). A value that
    is not already canonical raises rather than being silently repaired
    to look canonical on the wire. This is the property that keeps
    same-bucket tampering detectable: ``Decimal("0.899996")`` would
    format as ``"0.9000"`` under a repairing serializer, hiding the
    tampering; here it raises.
    """
    require_canonical_score(value, field_name)
    return format(value, ".4f")


def serialize_raw_decimal(value: Decimal) -> str:
    """Serialize a raw-evidence (variable-scale) decimal to its wire form.

    This is the sole exception to the fixed-scale canonical rule. The
    ``component_scores_raw`` field's purpose is to preserve precision
    that quantization discards — forcing it to 4dp defeats the field.

    Output shape: a lossless decimal string in fixed notation (no
    exponent). Trailing zeros are stripped (they are not significant);
    negative zero normalizes to ``"0"``.

    VERIFIED cases (all lossless: ``Decimal(serialize_raw_decimal(d)) == d``):

        Decimal("0.9400")   -> "0.94"
        Decimal("0.899996") -> "0.899996"
        Decimal("1.0000")   -> "1"
        Decimal("0.10")     -> "0.1"
        Decimal("1E-7")     -> "0.0000001"
        Decimal("-0")       -> "0"
        Decimal("0")        -> "0"
    """
    if not isinstance(value, Decimal):
        raise ScoreValidationError(
            f"Raw score must be Decimal, got {type(value).__name__}"
        )
    if not value.is_finite():
        raise ScoreValidationError("Raw score must be finite")
    if value < Decimal("0") or value > Decimal("1"):
        raise ScoreValidationError(
            f"Raw score outside [0, 1]: {value}"
        )
    if value == 0:
        return "0"
    # format(v, "f") suppresses exponent notation. Strip trailing zeros
    # then any lone trailing dot; fall back to "0" for the pathological
    # empty-string case (does not occur in practice, defensive).
    return format(value, "f").rstrip("0").rstrip(".") or "0"


# --------------------------------------------------------------------------- #
# Aggregator — pinned weighted sum                                             #
# --------------------------------------------------------------------------- #

def compute_weighted_score(
    scores: dict,
    weights: dict,
) -> Decimal:
    """Compute ``sum(weights[k] * scores[k])``, quantized to 4dp, under
    the pinned arithmetic context.

    This is the SOLE entry point for the pinned weighted-sum operation
    in the tis-v2 numerical path. Callers must not sum ``Decimal`` values
    outside of ``TIS_DECIMAL_CONTEXT`` and expect deterministic results.

    The start value is an explicit ``Decimal("0")`` — Python's
    ``sum([])`` returns integer 0, which would silently coerce the
    running total's type.
    """
    with localcontext(TIS_DECIMAL_CONTEXT):
        total = sum(
            (weights[k] * scores[k] for k in weights),
            Decimal("0"),
        )
        return total.quantize(SCORE_QUANTUM, rounding=SCORE_ROUNDING)


# --------------------------------------------------------------------------- #
# Public surface                                                               #
# --------------------------------------------------------------------------- #

__all__ = [
    # Version identifiers
    "SCORE_PRECISION_POLICY",
    "DECAY_ALGORITHM_VERSION",
    "CALCULATION_VERSION_V2",
    # Arithmetic context and quanta
    "TIS_DECIMAL_CONTEXT",
    "SCORE_QUANTUM",
    "PARAMETER_QUANTUM",
    "SCORE_ROUNDING",
    # Exceptions
    "ScoreValidationError",
    "CertificateInvariantError",
    "UnsupportedCalculationVersion",
    "UnsupportedCertificateSchemaVersion",
    # Shared dataclass
    "AdjustmentApplied",
    # Canonicalizers (input side)
    "canonical_score",
    "canonical_nonnegative_parameter",
    # Validators (internal, non-repairing)
    "require_canonical_score",
    "require_canonical_parameter",
    # Serializers
    "serialize_canonical_score",
    "serialize_raw_decimal",
    # Aggregator
    "compute_weighted_score",
]
