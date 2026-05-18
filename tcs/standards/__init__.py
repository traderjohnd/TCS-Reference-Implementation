"""
tcs.standards
==============

Phase 4 / Slice 4 — Standards-driven policy profile composition.

The standards library maps regulatory and industry frameworks to TCS
policy profile adjustments via explicit ``control_interpretation``
notes. The composer takes a base profile + selected standards and
produces a composed profile under hybrid / strictest-control rules.

Critical framing rule (locked):

    Standards do NOT mathematically require specific thresholds.
    A standard's ``profile_adjustments`` are this implementation's
    governance interpretation of how the standard's principles
    translate to TCS parameters. Every standard carries an explicit
    ``control_interpretation`` note saying so.

Composition rules (locked, hybrid / strictest-control):

    thresholds         : take the strictest (max) applicable value
    gate_set           : union of required gates
    required_controls  : OR logic (any standard's requirement applies)
    hard_prohibitions  : union
    penalty_weights    : additive with caps and re-normalization
    dimension_weights  : additive deltas allowed, re-normalized

This composition philosophy is more defensible than pure additive
stacking because selecting multiple standards should not arbitrarily
ratchet thresholds upward — instead the most restrictive relevant
control governs, while cumulative risk increases penalties and
weight emphasis.
"""

from __future__ import annotations

from tcs.standards.library import (
    STANDARDS,
    TAXONOMY,
    get_standard,
    list_standards,
    standards_for_use_case,
)
from tcs.standards.composer import (
    ComposedProfile,
    StandardContribution,
    compose_profile,
    composed_pack_id,
)

__all__ = [
    "STANDARDS",
    "TAXONOMY",
    "get_standard",
    "list_standards",
    "standards_for_use_case",
    "ComposedProfile",
    "StandardContribution",
    "compose_profile",
    "composed_pack_id",
]
