"""
tcs.workflow.events
====================

GovernanceEvent and BACK dimension signals (B/A/C/K).

Every connector in a workflow emits exactly one GovernanceEvent per
invocation. The event carries normalized evidence for each of the four
BACK governance dimensions plus execution metadata (latency, error,
payload reference). The GCA reads these events from the trace and
compiles them into a single TISInput for engine scoring.

Design principles
-----------------

1. **BACK, not BACU.** Boundedness, Attribution, Compliance, Known.
   K is a positive calibration score (higher = better calibrated).
   U exists only as a derived uncertainty quantity (U = 1 - K, or
   U_chain for CT-8 agent chains), never as a primary dimension.

2. **Connectors emit evidence, not scores.** A connector reports
   facts ("3 chunks retrieved, 2 missing source_doc"). The GCA
   converts evidence to scores using policy-aware rules. This keeps
   connectors interchangeable and the scoring math centralized.

3. **Immutable once emitted.** Events are frozen dataclasses.
   Hash-chain integrity fields (``event_hash``, ``previous_event_hash``)
   are populated by the orchestrator after emission and never mutated.
   Phase 5 will enforce a full chain; Slice 1 only carries the shape.

4. **Schema versioning.** ``EVENT_SCHEMA_VERSION`` is written into
   every event so future readers can interpret historical events
   correctly even as the schema evolves.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple

EVENT_SCHEMA_VERSION = "1.0"


# --------------------------------------------------------------------------- #
# Sensitivity tier classification (T0-T3)                                      #
# --------------------------------------------------------------------------- #

class SensitivityTier(str, Enum):
    """
    Data sensitivity classification per TCS_SPEC.md §19.

    T0 — Public / non-sensitive
    T1 — Internal / business-as-usual
    T2 — Confidential / restricted-access
    T3 — Regulated / highest sensitivity (PHI, PII, financial)
    """
    T0 = "T0"
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


# --------------------------------------------------------------------------- #
# BACK dimension signals                                                       #
# --------------------------------------------------------------------------- #
#
# Each signal carries the *evidence* needed to compute its dimension
# score. The GCA reads these and produces the final B, A, C, K scores
# the TIS engine consumes. Connectors should populate the fields they
# can observe directly; unset fields use safe defaults.
#
# All ``score_contribution`` fields are positive calibration values in
# [0, 1] where 1.0 means the connector saw no evidence reducing the
# dimension. This convention keeps every signal in the same numerical
# direction as the BACK model (higher = better).

@dataclass(frozen=True)
class BoundednessSignal:
    """
    Evidence contributing to the B (Boundedness) dimension.

    Boundedness asks: was the connector's output produced within the
    authorized operational scope, with no references to systems or
    data outside the governed context?
    """
    in_scope: bool = True
    scope_violations: Tuple[str, ...] = ()
    external_references: Tuple[str, ...] = ()
    score_contribution: float = 1.0


@dataclass(frozen=True)
class AttributionSignal:
    """
    Evidence contributing to the A (Attribution) dimension.

    Attribution asks: can every claim or piece of data be traced to a
    named, timestamped, versioned source? Integration boundary gaps
    are the dominant attribution failure mode in enterprise RAG.
    """
    source_count: int = 0
    sources_with_complete_metadata: int = 0
    integration_boundary_gaps: int = 0     # feeds P_cb penalty input
    timestamp_present: bool = True
    chain_of_custody_complete: bool = True
    score_contribution: float = 1.0


@dataclass(frozen=True)
class ComplianceSignal:
    """
    Evidence contributing to the C (Compliance) dimension.

    Compliance asks: does the output satisfy applicable regulatory
    and policy controls? The C3 sub-factor (prohibited pattern
    detection) is load-bearing: ``c3_violation=True`` produces a
    hard Stop that κ cannot override.
    """
    c3_violation: bool = False
    c3_pattern: Optional[str] = None
    restricted_content_detected: bool = False
    policy_violations: Tuple[str, ...] = ()
    documentation_complete: bool = True
    score_contribution: float = 1.0


@dataclass(frozen=True)
class KnownStateSignal:
    """
    Evidence contributing to the K (Known) dimension.

    Known asks: is the system's expressed confidence calibrated
    against the actual reliability of its inputs? Higher K means
    well-supported confidence; lower K means the system claims more
    than its inputs justify.

    Derived uncertainty quantities:
        U_derived = 1 - score_contribution
        U_chain   = 1 - product(K_i)  for CT-8 agent chains

    Both are derived, never primary dimensions.

    ``chain_k_scores`` is populated only for CT-8 (agent chain)
    connectors. Each value is a per-hop K_i in [0, 1]. CT-11
    (AI-generated attribution) does NOT use this field — chain math
    belongs to the CT-8 context.
    """
    confidence_calibrated: bool = True
    contradiction_count: int = 0
    novelty_score: float = 0.0          # 0.0 = familiar, 1.0 = novel
    dependency_acknowledged: bool = True
    confidence_interval_valid: bool = True
    score_contribution: float = 1.0
    # CT-8 only — left as None for non-chain connectors
    chain_k_scores: Optional[Tuple[float, ...]] = None


# --------------------------------------------------------------------------- #
# GovernanceEvent — the unit of evidence emitted by a connector                #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class GovernanceEvent:
    """
    Normalized governance evidence from a single connector invocation.

    The connector populates the BACK signals and execution metadata.
    The orchestrator populates ``event_hash`` and ``previous_event_hash``
    after the event is appended to the trace.

    Hash chain integrity is **shape-only** in Slice 1: the fields
    exist, the helper computes the hash correctly, but no enforcement
    runs at event emission. Phase 5 will enforce hash continuity
    across the trace and into the per-TC audit chain.

    Fields
    ------
    event_id
        UUID4 per event.
    workflow_id
        UUID4 identifying the workflow execution this event belongs to.
    node_id
        ID of the GovernedNode that produced this event.
    timestamp
        ISO-8601 UTC.
    connector_type
        Free-form connector label, e.g. "llm.openai", "rag.simple",
        "api.rest", "mcp.tool", "agent.chain". Used for telemetry.
    connection_type
        CT-1 .. CT-13 per TCS_SPEC.md §18. Drives policy resolution.
    sensitivity_tier
        T0-T3 classification of the data this connector handled.
    boundedness, attribution, compliance, known
        BACK dimension signals.
    payload_ref
        Optional opaque reference to the raw connector output
        (e.g. an object ID or storage key). The payload itself is
        deliberately NOT carried in the event — events are meant to
        be cheap to store and traverse.
    latency_ms, error
        Execution metadata.
    event_hash, previous_event_hash
        Hash chain shape. Populated by the orchestrator.
    schema_version
        Pinned to ``EVENT_SCHEMA_VERSION`` at construction.
    """
    event_id: str
    workflow_id: str
    node_id: str
    timestamp: str
    connector_type: str
    connection_type: str
    sensitivity_tier: str
    boundedness: BoundednessSignal
    attribution: AttributionSignal
    compliance: ComplianceSignal
    known: KnownStateSignal
    payload_ref: Optional[str] = None
    latency_ms: float = 0.0
    error: Optional[str] = None
    # Connector-specific telemetry the GCA may read for cross-cutting
    # governance signals (e.g. MCP context_expansion, TC reuse attempt,
    # API allowlist denials surfaced for audit). This is NOT a hatch
    # for scores — scores belong in the BACK signals above. This is
    # for boolean / categorical metadata that drives GCA-level rules
    # like invalidation. Keep it small and well-named.
    connector_metadata: Mapping[str, Any] = field(default_factory=dict)
    event_hash: Optional[str] = None
    previous_event_hash: Optional[str] = None
    schema_version: str = EVENT_SCHEMA_VERSION

    def to_dict(self) -> dict:
        """JSON-serializable representation of the event."""
        d = asdict(self)
        # Normalize tuples to lists for JSON.
        for sig in ("boundedness", "attribution", "compliance", "known"):
            sd = d[sig]
            for k, v in list(sd.items()):
                if isinstance(v, tuple):
                    sd[k] = list(v)
        # connector_metadata may be a MappingProxyType — convert to dict.
        if d.get("connector_metadata") is not None:
            d["connector_metadata"] = dict(d["connector_metadata"])
        return d

    def compute_hash(self) -> str:
        """
        SHA-256 hash of event content excluding the hash fields themselves.

        Excluded: ``event_hash``, ``previous_event_hash``. These are
        populated AFTER the hash is computed, so including them would
        be circular. Schema version is included so the hash is bound
        to the schema that produced it.
        """
        d = self.to_dict()
        d.pop("event_hash", None)
        d.pop("previous_event_hash", None)
        canonical = json.dumps(d, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
