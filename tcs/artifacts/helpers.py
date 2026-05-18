"""
tcs.artifacts.helpers
=====================

Pure functions used by both the dataclasses and the store:

  - ``normalize_prompt`` / ``hash_text`` — deterministic hashing so two
    artifacts with the same prompt or output produce the same hash and
    replay can verify "same captured content."
  - ``derive_enforcement_action`` — single source of truth for the
    (mode, decision) → enforcement_action mapping. The table is
    enforced both at GovernanceEvaluation construction time and in
    the test suite, so a future contributor can't accidentally let
    an "observe" evaluation block delivery.
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Final


# --------------------------------------------------------------------------- #
# Hashing                                                                      #
# --------------------------------------------------------------------------- #

def normalize_prompt(text: str) -> str:
    """
    Normalize a prompt/draft for hashing.

    Goals:
      - identical user intent → identical hash (cross-session dedup,
        replay equality)
      - trivial whitespace / unicode differences do not change the hash
      - preserve case + punctuation (semantic content)

    Steps:
      1. Unicode NFC normalization (decomposed → composed form)
      2. Collapse runs of whitespace into single spaces
      3. Strip leading/trailing whitespace
    """
    nfc = unicodedata.normalize("NFC", text)
    collapsed = " ".join(nfc.split())
    return collapsed.strip()


def hash_text(text: str) -> str:
    """
    SHA-256 hex digest of normalized text.

    Empty string and None inputs are treated distinctly by the caller —
    this function refuses None and returns the digest of the empty
    string for "". Callers should pass None straight through to the
    artifact's nullable hash field rather than calling this.
    """
    if text is None:
        raise TypeError("hash_text() requires a string; pass None through directly")
    normalized = normalize_prompt(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Enforcement-action derivation                                                #
# --------------------------------------------------------------------------- #

# Decision → enforcement_action mapping for evaluation mode "enforce".
# observe and what_if are handled in derive_enforcement_action with
# constant returns (logged_only and counterfactual_only respectively).

_ENFORCE_ACTION_BY_DECISION: Final[dict] = {
    "Allow":                "delivered",
    "Observe":              "delivered",   # r1 Observe ships with caveat
    "Hold":                 "held",
    "Escalate":             "escalated",
    "Stop":                 "blocked",
    # Phase 3 nine-outcome refinements — already used by decision_engine
    "Allow_with_logging":   "delivered",
    "Allow_with_redaction": "delivered",
    "Allow_with_step_up":   "held",        # held pending step-up
    "Rollback":             "blocked",
}


def derive_enforcement_action(mode: str, decision: str) -> str:
    """
    Single source of truth for the (mode, decision) → enforcement_action
    rule. Used by GovernanceEvaluation construction and by the
    architectural guardrail tests.

    Rules:
      - mode == "observe"  → always "logged_only" (no delivery
                             intervention, regardless of decision)
      - mode == "what_if"  → always "counterfactual_only" (no TC
                             issued; this evaluation will never affect
                             a live request)
      - mode == "enforce"  → decision drives the action per
                             _ENFORCE_ACTION_BY_DECISION

    Unknown modes raise ValueError so a typo cannot silently default to
    delivery.
    """
    if mode == "observe":
        return "logged_only"
    if mode == "what_if":
        return "counterfactual_only"
    if mode != "enforce":
        raise ValueError(
            f"unknown evaluation mode {mode!r}; "
            f"expected one of: observe, enforce, what_if"
        )

    action = _ENFORCE_ACTION_BY_DECISION.get(decision)
    if action is None:
        raise ValueError(
            f"unknown decision {decision!r}; "
            f"add it to _ENFORCE_ACTION_BY_DECISION if it's a new "
            f"decision outcome"
        )
    return action
