"""
tcs.providers.web_evidence
==========================

Provider-neutral web-retrieval evidence contract (demo-live branch,
Commit 5). Schema version ``web-evidence-v1``.

One explicit canonicalizer produces the deterministic byte
representation that is hashed into ``web_evidence_digest`` and bound
to the Trust Certificate through the bounded scope-attestation
summary. No ad hoc dictionary serialization: every field is emitted
explicitly (unavailable values use the contract's null/empty
representation — fields are never omitted per provider), ordering is
deterministic, and full downloaded pages / opaque encrypted provider
content are NEVER part of this structure.

Timestamp representation (documented): ISO-8601 UTC with a trailing
``Z`` (``2026-07-30T12:00:00.000000Z``). Provider-reported page-age /
publication strings are preserved verbatim as strings and are never
converted into fabricated precise timestamps.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

SCHEMA_VERSION = "web-evidence-v1"

# Bounded excerpt cap — citations carry the provider's returned excerpt
# only; this is a defensive ceiling, never an invitation to store pages.
MAX_CITED_TEXT_CHARS = 1000

RETRIEVAL_STATUSES = (
    "success", "partial", "retrieval_not_performed", "no_results",
    "no_citations", "retrieval_error", "paused", "provider_error",
    "empty_output",
)

#: Statuses eligible for governance + Trust Certificate issuance.
GOVERNABLE_STATUSES = ("success", "partial")


def utc_iso(dt: datetime) -> str:
    """The one documented canonical timestamp representation."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonicalize_url(url: Optional[str]) -> Optional[str]:
    """Canonical URL for deduplication and hashing.

    Rules (web-evidence-v1): lowercase scheme and hostname; strip
    default ports (80/http, 443/https); empty path -> "/"; drop the
    fragment; preserve path case; preserve query content AND ordering;
    tracking parameters are NOT removed (no documented rule identifies
    them). Non-HTTP(S) URLs return None — they are excluded from
    canonical identity (and from clickable rendering downstream); the
    original display URL is always retained separately.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    port = parts.port
    netloc = host
    if port is not None and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def query_hash(query: Optional[str]) -> Optional[str]:
    return sha256_hex(query) if query else None


@dataclass
class SearchAction:
    ordinal: int
    provider_call_id: Optional[str] = None
    action_type: str = "search"            # normalized
    provider_native_type: Optional[str] = None  # preserved, incl. unknown
    query: Optional[str] = None            # never invented
    status: str = "unknown"
    error_code: Optional[str] = None
    parent_call_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "provider_call_id": self.provider_call_id,
            "action_type": self.action_type,
            "provider_native_type": self.provider_native_type,
            "query": self.query,
            "query_hash": query_hash(self.query),
            "status": self.status,
            "error_code": self.error_code,
            "parent_call_id": self.parent_call_id,
        }


@dataclass
class ConsultedSource:
    first_seen_ordinal: int
    display_url: Optional[str] = None      # original provider value
    title: Optional[str] = None
    provider_source_id: Optional[str] = None
    page_age: Optional[str] = None         # provider string, verbatim
    source_type: Optional[str] = None
    cited: bool = False
    search_call_ids: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)  # bounded

    @property
    def canonical_url(self) -> Optional[str]:
        return canonicalize_url(self.display_url)

    @property
    def source_id(self) -> str:
        key = self.canonical_url or f"display:{self.display_url or ''}"
        return "src-" + sha256_hex(key)[:16]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "first_seen_ordinal": self.first_seen_ordinal,
            "display_url": self.display_url,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "provider_source_id": self.provider_source_id,
            "page_age": self.page_age,
            "source_type": self.source_type,
            "cited": self.cited,
            "search_call_ids": list(self.search_call_ids),
            "metadata": dict(sorted(self.metadata.items())),
        }


@dataclass
class Citation:
    ordinal: int
    source_display_url: Optional[str] = None
    provider_annotation_type: Optional[str] = None
    text_block_ordinal: int = 0
    start_offset: Optional[int] = None     # never invented
    end_offset: Optional[int] = None
    cited_text: Optional[str] = None       # provider excerpt, bounded
    title: Optional[str] = None

    def __post_init__(self) -> None:
        if self.cited_text and len(self.cited_text) > MAX_CITED_TEXT_CHARS:
            self.cited_text = self.cited_text[:MAX_CITED_TEXT_CHARS]

    @property
    def source_id(self) -> str:
        key = canonicalize_url(self.source_display_url) or \
            f"display:{self.source_display_url or ''}"
        return "src-" + sha256_hex(key)[:16]

    @property
    def citation_id(self) -> str:
        key = json.dumps([
            self.source_id, self.text_block_ordinal, self.start_offset,
            self.end_offset, self.ordinal,
        ], separators=(",", ":"))
        return "cit-" + sha256_hex(key)[:16]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "citation_id": self.citation_id,
            "source_id": self.source_id,
            "ordinal": self.ordinal,
            "provider_annotation_type": self.provider_annotation_type,
            "text_block_ordinal": self.text_block_ordinal,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "cited_text": self.cited_text,
            "title": self.title,
            "display_url": self.source_display_url,
        }


@dataclass
class WebRetrievalEvidence:
    provider: str
    model: str
    retrieval_status: str = "retrieval_not_performed"
    retrieval_mode: str = "live_web"
    # Three distinct truthful states (fixup after 5209e0b):
    #   live_access_requested  — derived from the OUTBOUND request
    #       configuration (e.g. OpenAI external_web_access: true);
    #       never inferred from the response.
    #   web_search_action_observed — derived from the provider
    #       RESPONSE: at least one search action was recorded
    #       (exposed as a derived property below).
    #   live_access_confirmed  — documented confirmation rule:
    #       True only when live access was explicitly requested AND at
    #       least one search action completed successfully. This does
    #       NOT claim every returned page was freshly fetched — the
    #       provider supplies no per-page freshness proof.
    live_access_requested: bool = True
    live_access_confirmed: Optional[bool] = None  # None = not knowable
    retrieval_started_at: Optional[str] = None    # utc_iso()
    retrieval_completed_at: Optional[str] = None
    provider_request_id: Optional[str] = None
    error_summary: Optional[str] = None           # sanitized, bounded
    answer_used_web_evidence: bool = False
    search_actions: List[SearchAction] = field(default_factory=list)
    consulted_sources: List[ConsultedSource] = field(default_factory=list)
    citations: List[Citation] = field(default_factory=list)

    # ── Derived counts ─────────────────────────────────────────────── #

    @property
    def search_call_count(self) -> int:
        return len(self.search_actions)

    @property
    def web_search_action_observed(self) -> bool:
        """Response-derived: at least one search action was recorded.
        Distinct from live_access_requested (request-derived) and from
        live_access_confirmed (documented confirmation rule)."""
        return len(self.search_actions) > 0

    @property
    def successful_search_count(self) -> int:
        return sum(1 for a in self.search_actions
                   if a.status in ("completed", "success"))

    @property
    def failed_search_count(self) -> int:
        return sum(1 for a in self.search_actions
                   if a.status not in ("completed", "success"))

    @property
    def consulted_source_count(self) -> int:
        return len(self.consulted_sources)

    @property
    def cited_source_count(self) -> int:
        return sum(1 for s in self.consulted_sources if s.cited)

    # ── Canonicalization ───────────────────────────────────────────── #

    def finalize(self) -> "WebRetrievalEvidence":
        """Apply web-evidence-v1 canonical ordering and deduplication.

        - search actions: by recorded ordinal;
        - consulted sources: dedup by canonical URL (display URL when
          non-HTTP(S)); earliest first_seen_ordinal wins; cited status
          merges by logical OR; search-call linkages merge first-seen
          order, deduplicated; bounded metadata is never concatenated
          (the earliest source's metadata is kept); ordered by
          (first_seen_ordinal, canonical identity);
        - citations: mark their sources cited, then order by
          (text_block_ordinal, provider start offset when present,
          citation ordinal).
        """
        self.search_actions.sort(key=lambda a: a.ordinal)

        merged: Dict[str, ConsultedSource] = {}
        for s in sorted(self.consulted_sources,
                        key=lambda s: s.first_seen_ordinal):
            key = s.source_id
            if key not in merged:
                merged[key] = s
                continue
            kept = merged[key]
            kept.cited = kept.cited or s.cited
            for cid in s.search_call_ids:
                if cid not in kept.search_call_ids:
                    kept.search_call_ids.append(cid)
        cited_ids = {c.source_id for c in self.citations}
        for s in merged.values():
            if s.source_id in cited_ids:
                s.cited = True
        self.consulted_sources = sorted(
            merged.values(),
            key=lambda s: (s.first_seen_ordinal,
                           s.canonical_url or f"display:{s.display_url or ''}"),
        )
        self.citations.sort(key=lambda c: (
            c.text_block_ordinal,
            c.start_offset if c.start_offset is not None else -1,
            c.ordinal,
        ))
        return self

    def to_dict(self) -> Dict[str, Any]:
        """Every field explicit — never omitted per provider."""
        return {
            "schema_version": SCHEMA_VERSION,
            "provider": self.provider,
            "model": self.model,
            "retrieval_mode": self.retrieval_mode,
            "retrieval_status": self.retrieval_status,
            "live_access_requested": self.live_access_requested,
            "web_search_action_observed": self.web_search_action_observed,
            "live_access_confirmed": self.live_access_confirmed,
            "retrieval_started_at": self.retrieval_started_at,
            "retrieval_completed_at": self.retrieval_completed_at,
            "search_call_count": self.search_call_count,
            "successful_search_count": self.successful_search_count,
            "failed_search_count": self.failed_search_count,
            "consulted_source_count": self.consulted_source_count,
            "cited_source_count": self.cited_source_count,
            "answer_used_web_evidence": self.answer_used_web_evidence,
            "provider_request_id": self.provider_request_id,
            "error_summary": self.error_summary,
            "search_actions": [a.to_dict() for a in self.search_actions],
            "consulted_sources": [s.to_dict()
                                  for s in self.consulted_sources],
            "citations": [c.to_dict() for c in self.citations],
        }


def canonical_evidence_bytes(evidence: WebRetrievalEvidence) -> bytes:
    """The single explicit canonicalizer for web-evidence-v1."""
    evidence.finalize()
    return json.dumps(
        evidence.to_dict(), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def evidence_digest(evidence: WebRetrievalEvidence) -> str:
    """web_evidence_digest = SHA-256(canonical_web_evidence_bytes)."""
    return hashlib.sha256(canonical_evidence_bytes(evidence)).hexdigest()


def evidence_dict_digest(evidence_dict: Dict[str, Any]) -> str:
    """Digest of an already-normalized evidence dict (the persisted
    artifact form). Identical canonical serialization as
    canonical_evidence_bytes, so replay can verify a stored evidence
    payload against the digest bound into the Trust Certificate."""
    canonical = json.dumps(evidence_dict, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def bounded_summary(evidence: WebRetrievalEvidence,
                    evidence_artifact_id: Optional[str]) -> Dict[str, Any]:
    """The bounded provenance summary bound into the Trust
    Certificate's hash-covered scope attestation. Never the source
    list, never citation payloads, never credentials."""
    evidence.finalize()  # counts must reflect canonical (merged) state
    return {
        "schema_version": SCHEMA_VERSION,
        "retrieval_mode": evidence.retrieval_mode,
        "provider": evidence.provider,
        "model": evidence.model,
        "search_call_count": evidence.search_call_count,
        "consulted_source_count": evidence.consulted_source_count,
        "cited_source_count": evidence.cited_source_count,
        "retrieval_status": evidence.retrieval_status,
        "web_evidence_digest": evidence_digest(evidence),
        "evidence_artifact_id": evidence_artifact_id,
    }


def compute_retrieval_status(evidence: WebRetrievalEvidence,
                             final_text: str,
                             paused: bool = False) -> str:
    """Truthful status per the Commit-5 success semantics (§9)."""
    if paused:
        return "paused"
    if evidence.search_call_count == 0:
        return "retrieval_not_performed"
    if evidence.successful_search_count == 0:
        return "retrieval_error"      # every search failed
    if evidence.consulted_source_count == 0:
        return "no_results"
    if not final_text:
        return "empty_output"
    if not evidence.citations:
        # Sources were consulted but the final answer carries no
        # citation — it cannot be presented as web-grounded output.
        return "no_citations"
    if evidence.failed_search_count > 0:
        return "partial"
    return "success"


__all__ = [
    "SCHEMA_VERSION", "GOVERNABLE_STATUSES", "RETRIEVAL_STATUSES",
    "MAX_CITED_TEXT_CHARS",
    "WebRetrievalEvidence", "SearchAction", "ConsultedSource", "Citation",
    "canonicalize_url", "query_hash", "utc_iso",
    "canonical_evidence_bytes", "evidence_digest", "evidence_dict_digest",
    "bounded_summary", "compute_retrieval_status",
]
