"""
demo/run_scenario.py
====================

CLI runner for the TCS v0.1 reference implementation.

Accepts a scenario JSON file, runs the full pipeline
(assemble_context -> compute_tis -> map_decision -> generate_certificate),
and prints a formatted evaluation summary. Pass --verbose to also dump
the full Trust Certificate as pretty-printed JSON.

Usage
-----
    python demo/run_scenario.py scenarios/healthcare_stop.json
    python demo/run_scenario.py scenarios/finance_allow.json --verbose
    python demo/run_scenario.py scenarios/decay_over_time.json -v

Scenario JSON schema
--------------------
    {
        "subject_id":         "cds-warfarin-001",
        "subject_type":       "recommendation",
        "policy_profile":     "clinical-cds-samed-v2",
        "dimension_scores":   {"B": 0.92, "A": 0.88, "C": 0.31, "K": 0.84},
        "sub_factor_scores":  {"C": {"C3": 0.00}},       // optional
        "context_metadata": {
            "n_gaps":              0,
            "context_age_hours":   0.1,
            "novelty_score":       0.05,
            "days_since_review":   2,
            "is_policy_sensitive": false,
            "blocking_context":    "warfarin_clarithromycin_GI_bleed"
        },
        "elapsed_hours":       0.0,
        "is_valid":            1,
        "invalidation_event":  null,
        "evaluation_time":     "2026-04-07T15:00:00Z",
        "evaluation_note":     "free-text audit note"     // optional, displayed
    }

Special case: scenarios can specify ``"elapsed_hours_variants": [0.0, 5.0, ...]``
instead of ``elapsed_hours`` to run the same scenario at multiple points in
time (used by the decay test).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow `python demo/run_scenario.py ...` from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tcs.policy_profiles import load_profile, PolicyProfile
from tcs.tis_engine import compute_tis, TISInput, TISResult
from tcs.decision_engine import map_decision
from tcs.trust_certificate import generate_certificate, TrustCertificate
from tcs.governed_context import assemble_context


# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

_THICK_LINE = "=" * 63
_THIN_LINE  = "-" * 63

_RISK_TIER_LABELS = {
    "r1": "r1 Low",
    "r2": "r2 Medium",
    "r3": "r3 High",
}

_ACTION_CLASS_LABELS = {
    "a1": "a1 Informational",
    "a2": "a2 Advisory",
    "a3": "a3 Operational",
    "a4": "a4 Regulated Decision",
}

_DIM_NAMES = {
    "B": "Boundedness",
    "A": "Attribution",
    "C": "Compliance",
    "K": "Known",
}


# --------------------------------------------------------------------------- #
# Scenario loading                                                             #
# --------------------------------------------------------------------------- #

def load_scenario(path: Path) -> Dict[str, Any]:
    """Load and lightly validate a scenario JSON file."""
    if not path.exists():
        raise FileNotFoundError(f"Scenario file not found: {path}")

    with path.open(encoding="utf-8") as f:
        try:
            scenario = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {path}: {e}") from e

    required = ("subject_id", "policy_profile", "dimension_scores")
    missing = [k for k in required if k not in scenario]
    if missing:
        raise ValueError(
            f"Scenario {path} is missing required fields: {missing}"
        )
    return scenario


def _parse_eval_time(raw: Optional[str]) -> datetime:
    """Parse an ISO-8601 evaluation_time or default to 2026-04-07T15:00:00Z."""
    if not raw:
        return datetime(2026, 4, 7, 15, 0, 0)
    # Accept trailing Z.
    raw = raw.rstrip("Z")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"Could not parse evaluation_time: {raw!r}")


def build_tis_input(
    scenario: Dict[str, Any],
    *,
    elapsed_hours: Optional[float] = None,
) -> TISInput:
    """
    Build a TISInput from a loaded scenario dict.

    If ``elapsed_hours`` is provided explicitly (e.g., when iterating over
    elapsed_hours_variants), it overrides the scenario's top-level value.
    """
    eff_elapsed = (
        elapsed_hours
        if elapsed_hours is not None
        else float(scenario.get("elapsed_hours", 0.0))
    )

    context_metadata = assemble_context(scenario.get("context_metadata"))

    return TISInput(
        subject_id=scenario["subject_id"],
        subject_type=scenario.get("subject_type", "recommendation"),
        policy_profile=load_profile(scenario["policy_profile"]),
        dimension_scores=dict(scenario["dimension_scores"]),
        sub_factor_scores=dict(scenario.get("sub_factor_scores", {})),
        context_metadata=context_metadata,
        elapsed_hours=eff_elapsed,
        is_valid=int(scenario.get("is_valid", 1)),
        invalidation_event=scenario.get("invalidation_event"),
        evaluation_time=_parse_eval_time(scenario.get("evaluation_time")),
    )


# --------------------------------------------------------------------------- #
# Pretty printing                                                              #
# --------------------------------------------------------------------------- #

def _format_dimension_line(
    dim: str,
    score: float,
    result_status: str,
    threshold: float,
    is_c3_hard_stop: bool = False,
) -> str:
    """Build one dimension row for the DIMENSION SCORES block."""
    label = f"{dim} {_DIM_NAMES[dim]}"
    score_str = f"{score:.4f}"

    if result_status == "pass":
        verdict = f"PASS (>= {threshold:.2f})"
    elif result_status == "fail":
        verdict = f"FAIL (< {threshold:.2f})"
    else:  # not_applicable
        verdict = "not_gated"

    suffix = "  <- C3 = 0.00" if is_c3_hard_stop else ""
    return f"  {label:<26s} {score_str}  {verdict}{suffix}"


def print_summary(
    scenario: Dict[str, Any],
    tis_input: TISInput,
    tis_result: TISResult,
    decision: str,
    requires_human_review: bool,
    tc: TrustCertificate,
) -> None:
    """Pretty-print the evaluation summary to stdout."""
    profile: PolicyProfile = tis_input.policy_profile

    print(_THICK_LINE)
    print("TRUST COMPUTATION SYSTEM -- EVALUATION RESULT")
    print(_THICK_LINE)

    # --- Header --- #
    print(f"Subject:    {tis_input.subject_id} ({tis_input.subject_type})")
    print(
        f"Domain:     {profile.domain} | "
        f"{_RISK_TIER_LABELS.get(profile.risk_tier, profile.risk_tier)} | "
        f"{_ACTION_CLASS_LABELS.get(profile.action_class, profile.action_class)}"
    )
    print(f"Policy:     {profile.profile_id}")

    if tis_input.elapsed_hours > 0:
        print(f"Elapsed:    {tis_input.elapsed_hours:.2f} hours since t0")
    if scenario.get("evaluation_note"):
        print(f"Note:       {scenario['evaluation_note']}")

    # --- Dimension scores --- #
    print(_THIN_LINE)
    print("DIMENSION SCORES")

    c3_is_hard_stop = (tis_result.C3_score == 0.00)
    for dim in ("B", "A", "C", "K"):
        score = tis_input.dimension_scores[dim]
        threshold = profile.thresholds[dim]
        status = tis_result.gate_results_by_dim[dim]
        print(_format_dimension_line(
            dim, score, status, threshold,
            is_c3_hard_stop=(dim == "C" and c3_is_hard_stop),
        ))

    # --- TIS computation --- #
    print(_THIN_LINE)
    print("TIS COMPUTATION")
    print(f"  TIS_raw:                 {tis_result.tis_raw:.4f}")
    print(f"  Penalty aggregate (P):   {tis_result.penalty_aggregate:.4f}")
    _print_penalty_breakdown(tis_result.penalty_breakdown)
    print(f"  TIS_adj:                 {tis_result.tis_adj:.4f}")
    print(f"  Decay factor:            {tis_result.decay_factor:.4f}")
    gate_label = "PASS" if tis_result.gate_result == 1 else "FAIL"
    print(f"  Gate result:             {tis_result.gate_result} ({gate_label})")
    if tis_result.is_valid == 0:
        print(f"  Invalidation:            YES ({tis_result.invalidation_event})")
    print(f"  TIS_current:             {tis_result.tis_current:.4f}")

    # --- Decision --- #
    print(_THIN_LINE)
    marker = _decision_marker(decision)
    print(f"DECISION:  {marker} {decision.upper()} {marker}")
    if tc.blocking_reason:
        print(f"  Blocking reason:  {tc.blocking_reason}")
    review_text = _review_text(decision, requires_human_review)
    print(f"  Human review:     {review_text}")
    print(f"  Lifecycle state:  {tc.lifecycle_state}")
    if tc.escalation_routed_to:
        print(f"  Routed to:        {', '.join(tc.escalation_routed_to)}")

    # --- Trust Certificate --- #
    print(_THIN_LINE)
    print("TRUST CERTIFICATE")
    print(f"  ID:             {tc.certificate_id}")
    issued = tc.evaluation_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    valid  = tc.valid_until.strftime("%Y-%m-%dT%H:%M:%SZ")
    half_life_hours = math.log(2.0) / profile.decay_rate
    print(f"  Issued:         {issued}")
    print(f"  Valid until:    {valid} (half-life: {half_life_hours:.2f} hrs)")
    if tc.regulatory_mapping:
        reg = " | ".join(tc.regulatory_mapping[:3])
        if len(tc.regulatory_mapping) > 3:
            reg += f" | (+{len(tc.regulatory_mapping) - 3} more)"
        print(f"  Reg mapping:    {reg}")
    else:
        print(f"  Reg mapping:    (none - internal use)")

    print(_THICK_LINE)


def _print_penalty_breakdown(breakdown: Dict[str, float]) -> None:
    """Print the non-zero penalty components as a compact sublist."""
    nonzero = [(k, v) for k, v in breakdown.items() if v > 0.0]
    if not nonzero:
        print("    (all components 0.0000)")
        return
    for key, value in nonzero:
        print(f"    {key:<6s} = {value:.4f}")


def _decision_marker(decision: str) -> str:
    """Short status marker shown next to the decision line."""
    return {
        "Allow":    "[+]",
        "Observe":  "[o]",
        "Hold":     "[!]",
        "Escalate": "[!!]",
        "Stop":     "[X]",
    }.get(decision, "[?]")


def _review_text(decision: str, review: bool) -> str:
    if decision == "Stop":
        return "No (hard stop -- no human override)"
    if review:
        return "Yes"
    return "No"


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #

def _run_one(
    scenario: Dict[str, Any],
    *,
    elapsed_hours: Optional[float] = None,
    verbose: bool = False,
) -> None:
    tis_input = build_tis_input(scenario, elapsed_hours=elapsed_hours)
    tis_result = compute_tis(tis_input)
    decision, review = map_decision(tis_input, tis_result)
    tc = generate_certificate(tis_input, tis_result, decision, review)

    print_summary(scenario, tis_input, tis_result, decision, review, tc)

    if verbose:
        print()
        print("FULL TRUST CERTIFICATE (JSON):")
        print(tc.to_json(indent=2))
        print()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a TCS scenario JSON file through the full pipeline.",
    )
    parser.add_argument(
        "scenario",
        type=Path,
        help="Path to a scenario JSON file (e.g. scenarios/healthcare_stop.json).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Also dump the full Trust Certificate as pretty JSON.",
    )
    args = parser.parse_args(argv)

    try:
        scenario = load_scenario(args.scenario)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    # If scenario specifies elapsed_hours_variants, iterate over them
    # (used by the decay test scenario).
    variants = scenario.get("elapsed_hours_variants")
    if variants:
        for i, eh in enumerate(variants):
            if i > 0:
                print()
            print(f"### elapsed_hours = {eh} ###")
            _run_one(scenario, elapsed_hours=float(eh), verbose=args.verbose)
    else:
        _run_one(scenario, verbose=args.verbose)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
