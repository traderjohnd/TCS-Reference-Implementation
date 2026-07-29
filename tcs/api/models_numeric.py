"""
tcs.api.models_numeric
======================

Decimal-string transport types for governed numerical fields (tis-v2
Commit 5a, owner correction 1).

Under the ordinary FastAPI/JSON parsing path a JSON *number* token may
pass through a binary floating-point representation before validation
sees it — so a Pydantic ``Decimal`` annotation alone cannot prove
lexical fidelity. Every externally supplied numerical value whose
decimal fidelity matters is therefore a JSON **string** on the wire:

    {"identity_confidence": "0.899996", "similarity_score": "0.923456"}

The boundary contract, enforced here:

    * only a decimal string is accepted — JSON numeric tokens are
      rejected (they may already be binary floats);
    * booleans rejected;
    * exponent notation rejected;
    * leading/trailing whitespace, signs, NaN, Infinity, and malformed
      strings rejected;
    * the submitted decimal value is preserved exactly — never
      quantized or repaired;
    * fixed-scale (4dp) validation applies only where a field's wire
      contract is fixed-scale.

OpenAPI: fields using these types are documented as ``string`` with the
matching pattern — never ``number | string``.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

#: Variable-scale governed decimal: plain decimal notation, no sign, no
#: exponent, no whitespace. "0", "1", "0.899996", "12.5" are valid.
GOVERNED_DECIMAL_PATTERN = r"^(?:0|[1-9]\d*)(?:\.\d+)?$"
_GOVERNED_DECIMAL_RE = re.compile(GOVERNED_DECIMAL_PATTERN)

#: Fixed-scale 4dp variant for fields whose wire contract is 4dp.
FIXED_4DP_PATTERN = r"^(?:0|[1-9]\d*)\.\d{4}$"
_FIXED_4DP_RE = re.compile(FIXED_4DP_PATTERN)


class GovernedDecimalError(ValueError):
    """Raised when an inbound governed decimal violates the contract."""


def parse_governed_decimal(
    value: object,
    field_name: str,
    *,
    minimum: Optional[Decimal] = None,
    maximum: Optional[Decimal] = None,
    fixed_4dp: bool = False,
) -> Decimal:
    """Parse an externally supplied governed decimal STRING.

    Accepts only ``str``. Never repairs: a malformed value is the
    caller's error, reported as such.
    """
    if isinstance(value, bool):
        raise GovernedDecimalError(
            f"{field_name}: boolean is not a decimal value"
        )
    if not isinstance(value, str):
        raise GovernedDecimalError(
            f"{field_name}: governed decimal fields must be JSON strings "
            f"(e.g. \"0.899996\") to preserve lexical fidelity; got "
            f"{type(value).__name__}"
        )
    pattern = _FIXED_4DP_RE if fixed_4dp else _GOVERNED_DECIMAL_RE
    if not pattern.fullmatch(value):
        raise GovernedDecimalError(
            f"{field_name}: not a canonical decimal string: {value!r}"
        )
    d = Decimal(value)   # pattern guarantees finite, non-negative, no exponent
    if minimum is not None and d < minimum:
        raise GovernedDecimalError(
            f"{field_name}: below minimum {minimum}: {value!r}"
        )
    if maximum is not None and d > maximum:
        raise GovernedDecimalError(
            f"{field_name}: above maximum {maximum}: {value!r}"
        )
    return d


def parse_unit_interval_decimal(value: object, field_name: str) -> Decimal:
    """Governed decimal constrained to [0, 1] (scores, confidences,
    similarities)."""
    return parse_governed_decimal(
        value, field_name,
        minimum=Decimal("0"), maximum=Decimal("1"),
    )


__all__ = [
    "GOVERNED_DECIMAL_PATTERN",
    "FIXED_4DP_PATTERN",
    "GovernedDecimalError",
    "parse_governed_decimal",
    "parse_unit_interval_decimal",
]
