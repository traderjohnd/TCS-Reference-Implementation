"""
tcs.api.routes_certificates
===========================

Trust Certificate read surface.

Endpoints
---------
GET /v2/certificates                    list recent TCs across all chains
GET /v2/certificates/verify-chain       verify hash chain integrity
GET /v2/certificates/{certificate_id}   fetch a single TC by id

The list and verify-chain routes are declared *before* the
``{certificate_id}`` route so FastAPI does not match the literal
path segments as ids.

These endpoints are the "show me the receipts" surface. Regulatory
and audit consumers call them with ids they got from a prior
/v2/govern response, or browse the recent feed in the dashboard.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from tcs.persistence import CertificateNotFoundError


router = APIRouter()


# --------------------------------------------------------------------------- #
# Recent list                                                                  #
# --------------------------------------------------------------------------- #

@router.get("/certificates")
def list_certificates(
    request: Request,
    limit: int = Query(20, ge=1, le=200),
) -> Dict[str, Any]:
    """
    Return the most recent committed TCs across all chains.

    Used by the dashboard live-decisions feed. Each item is the full
    ``TrustCertificate.to_dict()`` shape.
    """
    store = request.app.state.store
    tcs = store.list_recent(limit=limit)
    return {
        "count": len(tcs),
        "certificates": [tc.to_dict() for tc in tcs],
    }


# --------------------------------------------------------------------------- #
# Chain verification                                                           #
# --------------------------------------------------------------------------- #

@router.get("/certificates/verify-chain")
def verify_chain(
    request: Request,
    chain_id: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """
    Verify hash chain integrity.

    If ``chain_id`` is supplied, verifies just that chain. If omitted,
    verifies *every* chain in the archive and returns the aggregate
    result. Returns ``chain_intact`` (bool) plus ``tc_count`` so the
    dashboard can render a "X TCs verified" line.
    """
    store = request.app.state.store

    if chain_id is not None:
        ok = store.verify_chain(chain_id)
        tc_count = len(store.list_chain(chain_id))
        return {
            "chain_intact": bool(ok),
            "chain_id": chain_id,
            "tc_count": tc_count,
        }

    chain_ids: List[str] = store.list_chain_ids()
    all_ok = True
    broken: List[str] = []
    for cid in chain_ids:
        if not store.verify_chain(cid):
            all_ok = False
            broken.append(cid)

    return {
        "chain_intact": all_ok,
        "chain_count": len(chain_ids),
        "tc_count": store.count(),
        "broken_chains": broken,
    }


# --------------------------------------------------------------------------- #
# Chain summary                                                                #
# --------------------------------------------------------------------------- #

@router.get("/certificates/chain/{chain_id}/summary")
def chain_summary(
    chain_id: str,
    request: Request,
) -> Dict[str, Any]:
    """
    Return summary for a specific chain including length, timestamps,
    decision counts, and verification status.
    """
    store = request.app.state.store
    return store.chain_summary(chain_id)


# --------------------------------------------------------------------------- #
# Chain walk — full audit walk-through                                         #
# --------------------------------------------------------------------------- #

@router.get("/certificates/chain/{chain_id}/walk")
def chain_walk(
    chain_id: str,
    request: Request,
) -> Dict[str, Any]:
    """
    Return the ordered list of TCs in a chain with per-row integrity
    checks, suitable for an examiner-grade hash-chain walk-through.

    For each TC (ordered by chain_sequence ASC) the response includes:

      certificate_id       — UUID of this TC
      decision             — Allow / Hold / Stop / etc.
      evaluation_timestamp — ISO-8601 UTC
      lifecycle_state      — admissible / computed / blocked / observed / ...
      chain_sequence       — 1, 2, 3, ...
      tc_hash              — stored SHA-256 hash of the TC content
      previous_tc_hash     — stored hash of the prior TC in the chain
                              (null for chain_sequence==1)
      content_hash_ok      — True iff recompute(tc_dict) == stored tc_hash
                              (proves the TC content hasn't been altered)
      linkage_ok           — True iff previous_tc_hash matches the prior
                              TC's tc_hash (proves no row was inserted,
                              removed, or reordered)
      sequence_ok          — True iff chain_sequence == expected (proves
                              no row was dropped)

    Top-level fields:

      chain_id             — echoed for convenience
      count                — number of TCs in the chain
      chain_intact         — True iff every per-row check passes; equivalent
                              to ``CertificateStore.verify_chain(chain_id)``
                              but with the per-row evidence exposed so the
                              UI can show WHICH row failed.

    Returns 200 with ``count = 0`` and an empty list for an unknown
    chain (matches the idiom used elsewhere — we don't 404 on absence
    of evidence because that conflates "chain is empty" with "chain
    doesn't exist", and the existing chain_id selector only offers
    chains that do exist).
    """
    from tcs.canonical import (
        CertificateInvariantError,
        UnsupportedCertificateSchemaVersion,
    )
    from tcs.trust_certificate import compute_legacy_raw_tc_hash

    store = request.app.state.store
    tcs = store.list_chain(chain_id)
    # Content hashes are verified against the RAW persisted dictionaries
    # (before rehydration), keyed by certificate_id. The raw contents are
    # used ONLY for hash recomputation and are never exposed in the
    # response — display fields come from the rehydrated TCs below.
    raw_by_id = {
        raw.get("certificate_id"): raw
        for raw in store._list_chain_raw(chain_id)
    }
    if not tcs:
        return {
            "chain_id": chain_id,
            "count": 0,
            "chain_intact": True,   # vacuously true; matches verify_chain
            "rows": [],
        }

    rows = []
    prev_hash: Optional[str] = None
    expected_seq = 1
    chain_intact = True
    for tc in tcs:
        ai = tc.audit_integrity
        if ai is None:
            # No audit layer at all — every check fails for this row.
            rows.append({
                "certificate_id":       tc.certificate_id,
                "decision":             tc.decision,
                "evaluation_timestamp": tc.evaluation_timestamp.strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "lifecycle_state":      tc.lifecycle_state,
                "chain_sequence":       None,
                "tc_hash":              None,
                "previous_tc_hash":     None,
                "content_hash_ok":      False,
                "linkage_ok":           False,
                "sequence_ok":          False,
            })
            chain_intact = False
            continue

        # (a) content hash verified from the raw persisted dict
        raw = raw_by_id.get(tc.certificate_id)
        if raw is None:
            content_hash_ok = False
        else:
            try:
                content_hash_ok = (
                    compute_legacy_raw_tc_hash(raw) == ai.tc_hash
                )
            except (CertificateInvariantError,
                    UnsupportedCertificateSchemaVersion):
                content_hash_ok = False

        # (b) previous_tc_hash linkage
        if expected_seq == 1:
            linkage_ok = (ai.previous_tc_hash is None)
        else:
            linkage_ok = (ai.previous_tc_hash == prev_hash)

        # (c) monotonic sequence
        sequence_ok = (ai.chain_sequence == expected_seq)

        rows.append({
            "certificate_id":       tc.certificate_id,
            "decision":             tc.decision,
            "evaluation_timestamp": tc.evaluation_timestamp.strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "lifecycle_state":      tc.lifecycle_state,
            "chain_sequence":       ai.chain_sequence,
            "tc_hash":              ai.tc_hash,
            "previous_tc_hash":     ai.previous_tc_hash,
            "content_hash_ok":      content_hash_ok,
            "linkage_ok":           linkage_ok,
            "sequence_ok":          sequence_ok,
        })
        if not (content_hash_ok and linkage_ok and sequence_ok):
            chain_intact = False

        prev_hash = ai.tc_hash
        expected_seq += 1

    return {
        "chain_id":     chain_id,
        "count":        len(rows),
        "chain_intact": chain_intact,
        "rows":         rows,
    }


# --------------------------------------------------------------------------- #
# Single TC by id                                                              #
# --------------------------------------------------------------------------- #

@router.get("/certificates/{certificate_id}")
def get_certificate(
    certificate_id: str,
    request: Request,
) -> Dict[str, Any]:
    """
    Return the full Trust Certificate for ``certificate_id``.

    Returns the same dict shape as ``TrustCertificate.to_dict()``,
    including all 11 layers and the CT audit fields. The stored
    ``tc_hash`` lets the caller verify integrity by recomputing
    via ``compute_tc_hash()``.

    Raises HTTP 404 if no TC matches the id.
    """
    store = request.app.state.store
    try:
        tc = store.get(certificate_id)
    except CertificateNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"No certificate with id={certificate_id!r}",
        )
    return tc.to_dict()
