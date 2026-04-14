"""
tcs.api.routes_certificates
===========================

Trust Certificate read surface.

Endpoints
---------
GET /v1/certificates                    list recent TCs across all chains
GET /v1/certificates/verify-chain       verify hash chain integrity
GET /v1/certificates/{certificate_id}   fetch a single TC by id

The list and verify-chain routes are declared *before* the
``{certificate_id}`` route so FastAPI does not match the literal
path segments as ids.

These endpoints are the "show me the receipts" surface. Regulatory
and audit consumers call them with ids they got from a prior
/v1/govern response, or browse the recent feed in the dashboard.
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
