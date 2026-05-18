"""
tcs.persistence.certificate_store
=================================

High-level persistence API for Trust Certificates.

The :class:`CertificateStore` class wraps the SQLite schema defined in
``tcs.persistence.db`` with three guarantees:

    1. **Append-only**: ``issue()`` inserts a new row per TC. There is
       no update() and no delete(). Corrections go through
       ``amend()``, which writes a *new* TC that references the old
       one — the old row stays exactly as it was (C-R.18, C-P.14/15).

    2. **Chain sequencing**: each TC is assigned a ``chain_id`` +
       ``chain_sequence`` (strictly monotonic +1 per chain_id) and a
       ``previous_tc_hash`` linking it to its predecessor. The first
       TC in a chain has ``chain_sequence=1`` and ``previous_tc_hash=
       None``. The store assigns these automatically if the TC does
       not already carry them.

    3. **Chain verification**: ``verify_chain(chain_id)`` walks every
       TC in the chain, recomputes its tc_hash from the stored
       content_json, and checks that previous_tc_hash linkages and
       chain_sequence monotonicity are intact. Returns True only if
       *everything* checks out. This is the Phase-1 Step 1 acceptance
       gate: three sequential TCs verify_chain() → True.

The store never mutates the :class:`TrustCertificate` instances passed
in. It returns a fresh TC object built from the issued row so callers
can see the final assigned chain linkage without touching their
original.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

from tcs.trust_certificate import (
    AuditIntegrity,
    TrustCertificate,
    compute_tc_hash,
)
from tcs.persistence.db import (
    AppendOnlyViolation,
    init_db,
    open_connection,
    translate_append_only_error,
)


class ChainSequenceError(RuntimeError):
    """
    Raised when an incoming TC's chain linkage is inconsistent with
    what is already in the store — e.g. chain_sequence gap, wrong
    previous_tc_hash, or duplicate (chain_id, chain_sequence).
    """


class CertificateNotFoundError(LookupError):
    """Raised when ``get()`` cannot find a certificate_id in the store."""


# --------------------------------------------------------------------------- #
# CertificateStore                                                             #
# --------------------------------------------------------------------------- #

class CertificateStore:
    """
    Thread-unsafe by design (Phase 2 prototype). One store per thread.

    Typical lifecycle:

        store = CertificateStore("data/tcs.db")
        issued = store.issue(tc)                    # writes row, returns new TC
        loaded = store.get(issued.certificate_id)   # round-trips through JSON
        ok     = store.verify_chain(chain_id)       # True iff chain intact
        store.close()

    Or as a context manager:

        with CertificateStore("data/tcs.db") as store:
            store.issue(tc)
    """

    def __init__(
        self,
        db_path: Union[str, Path, None] = None,
        *,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        if conn is not None:
            self._conn = init_db(conn=conn)
            self._owns_conn = False
        else:
            self._conn = init_db(db_path)
            self._owns_conn = True

    # ---- Context manager plumbing --------------------------------------- #

    def __enter__(self) -> "CertificateStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()

    def close(self) -> None:
        if self._owns_conn and self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None  # type: ignore[assignment]

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """
        Wrap a block in an explicit BEGIN/COMMIT. On any exception we
        ROLLBACK so a failed insert does not leave a half-committed TC.
        """
        self._conn.execute("BEGIN")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ---- Core: issue ---------------------------------------------------- #

    def issue(self, tc: TrustCertificate) -> TrustCertificate:
        """
        Assign chain linkage, (re)compute the hash, and append the TC.

        Steps, in order:

            1. Determine chain_id: if the incoming TC already carries
               one via its audit_integrity layer, use it; otherwise
               reuse the existing chain_id from the TC.
            2. Look up the last TC in that chain (if any).
            3. Assign chain_sequence = last.chain_sequence + 1 (or 1).
            4. Assign previous_tc_hash = last.tc_hash (or None).
            5. Recompute tc_hash over the TC content with the updated
               audit_integrity layer attached (which is still excluded
               from the hash input — see compute_tc_hash).
            6. INSERT the row inside a transaction.

        Returns a **new** TrustCertificate instance with the updated
        audit_integrity layer. The caller's TC is not mutated.

        Raises:
            ChainSequenceError — linkage integrity could not be satisfied
            AppendOnlyViolation — triggered schema refused the write
        """
        if tc.audit_integrity is None:
            # Phase 1's generate_certificate() always populates audit_integrity,
            # but defensive for direct callers.
            raise ChainSequenceError(
                "Cannot issue a TC with audit_integrity=None; "
                "call generate_certificate() first"
            )

        with self._transaction() as conn:
            chain_id = tc.audit_integrity.chain_id
            last = self._last_in_chain_locked(conn, chain_id)

            if last is None:
                new_sequence = 1
                new_previous_hash: Optional[str] = None
            else:
                new_sequence = int(last["chain_sequence"]) + 1
                new_previous_hash = str(last["tc_hash"])

            # Build a fresh AuditIntegrity with the store-assigned chain
            # linkage. Tc_hash is recomputed after we know the linkage,
            # because previous_tc_hash and chain_sequence affect it only
            # insofar as they appear in other layers — compute_tc_hash
            # *excludes* the audit layer itself, so changing AI fields
            # does not affect the hash. This means: the hash computed
            # from the caller's TC is invariant under chain assignment.
            new_audit = AuditIntegrity(
                tc_hash=tc.audit_integrity.tc_hash,  # provisional; recomputed below
                previous_tc_hash=new_previous_hash,
                chain_sequence=new_sequence,
                chain_id=chain_id,
                hash_algorithm=tc.audit_integrity.hash_algorithm,
                integrity_verified=True,
                issued_by=tc.audit_integrity.issued_by,
            )

            # Construct a new TC instance so we do not mutate the caller's.
            # dataclasses.replace preserves every field we did not touch.
            issued_tc = dataclass_replace(tc, audit_integrity=new_audit)

            # Recompute the hash over the full to_dict() — this is stable
            # because compute_tc_hash drops the "audit_integrity" key.
            final_hash = compute_tc_hash(issued_tc.to_dict())
            issued_tc.audit_integrity = AuditIntegrity(
                tc_hash=final_hash,
                previous_tc_hash=new_previous_hash,
                chain_sequence=new_sequence,
                chain_id=chain_id,
                hash_algorithm=new_audit.hash_algorithm,
                integrity_verified=True,
                issued_by=new_audit.issued_by,
            )

            self._insert_tc_locked(conn, issued_tc)

            return issued_tc

    # ---- Retrieval ------------------------------------------------------ #

    def get(self, certificate_id: str) -> TrustCertificate:
        """
        Re-hydrate a TC from its stored content_json.

        Raises :class:`CertificateNotFoundError` if no row matches.
        """
        row = self._conn.execute(
            "SELECT content_json FROM trust_certificates "
            "WHERE certificate_id = ?",
            (certificate_id,),
        ).fetchone()
        if row is None:
            raise CertificateNotFoundError(
                f"No certificate with certificate_id={certificate_id!r}"
            )
        return _tc_from_json(row["content_json"])

    def list_chain(self, chain_id: str) -> List[TrustCertificate]:
        """
        Return every TC in a chain, ordered by chain_sequence.

        The returned list is empty if no TCs exist for the chain_id —
        verify_chain() will still return True for empty input (vacuous).
        """
        rows = self._conn.execute(
            "SELECT content_json FROM trust_certificates "
            "WHERE chain_id = ? ORDER BY chain_sequence ASC",
            (chain_id,),
        ).fetchall()
        return [_tc_from_json(r["content_json"]) for r in rows]

    def count(self) -> int:
        """Return total number of TCs in the archive."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM trust_certificates"
        ).fetchone()
        return int(row["n"])

    def list_recent(self, limit: int = 20) -> List[TrustCertificate]:
        """
        Return the most recently committed TCs across all chains,
        ordered by evaluation_timestamp DESC. Used by the dashboard
        feed to show live activity.
        """
        rows = self._conn.execute(
            "SELECT content_json FROM trust_certificates "
            "ORDER BY evaluation_timestamp DESC, chain_sequence DESC "
            "LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [_tc_from_json(r["content_json"]) for r in rows]

    # ---- Windowed queries (Phase 3 dynamics) ------------------------------ #

    def _compute_window_cutoff(self, window_hours: float) -> str:
        """
        Compute the cutoff timestamp for a sliding window query.

        Uses the latest evaluation_timestamp in the store as the anchor
        point, falling back to ``now(UTC)`` when the store is empty.
        This ensures windowed queries work correctly regardless of when
        the data was written (e.g. test fixtures with hardcoded dates).
        """
        from datetime import datetime, timezone, timedelta

        row = self._conn.execute(
            "SELECT MAX(evaluation_timestamp) AS latest "
            "FROM trust_certificates"
        ).fetchone()
        latest_ts = row["latest"] if row and row["latest"] else None

        if latest_ts is not None:
            # Parse the stored ISO-8601 timestamp
            # Handle both 'Z' suffix and '+00:00' formats
            ts_str = latest_ts.replace("Z", "+00:00")
            try:
                anchor = datetime.fromisoformat(ts_str)
            except ValueError:
                anchor = datetime.now(timezone.utc)
        else:
            anchor = datetime.now(timezone.utc)

        cutoff = (anchor - timedelta(hours=window_hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return cutoff

    def query_window(
        self,
        window_hours: float,
        *,
        since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Return denormalized rows for TCs within a time window.

        Each row is a dict with keys: tis_current, decision,
        evaluation_timestamp, content_json. If ``since`` is provided
        (ISO-8601 string), it is used as the cutoff; otherwise cutoff
        is computed as ``now - window_hours``.

        Returns rows ordered by evaluation_timestamp ASC.
        """
        if since is None:
            cutoff = self._compute_window_cutoff(window_hours)
        else:
            cutoff = since

        rows = self._conn.execute(
            "SELECT tis_current, decision, evaluation_timestamp, content_json "
            "FROM trust_certificates "
            "WHERE evaluation_timestamp >= ? "
            "ORDER BY evaluation_timestamp ASC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    def query_window_by_context(
        self,
        window_hours: float,
        *,
        since: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Return windowed TC rows grouped by governance context
        (policy_set_id).

        Each row is a dict with keys: tis_current, decision,
        evaluation_timestamp, policy_set_id, content_json.
        Groups are keyed by policy_set_id.
        """
        if since is None:
            cutoff = self._compute_window_cutoff(window_hours)
        else:
            cutoff = since

        rows = self._conn.execute(
            "SELECT tis_current, decision, evaluation_timestamp, "
            "       policy_set_id, content_json "
            "FROM trust_certificates "
            "WHERE evaluation_timestamp >= ? "
            "ORDER BY evaluation_timestamp ASC",
            (cutoff,),
        ).fetchall()

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            d = dict(r)
            ctx = d["policy_set_id"]
            grouped.setdefault(ctx, []).append(d)
        return grouped

    # ---- Aggregation helpers (Phase 2 metrics/health endpoints) ------- #

    def list_chain_ids(self) -> List[str]:
        """Return every distinct chain_id in the archive."""
        rows = self._conn.execute(
            "SELECT DISTINCT chain_id FROM trust_certificates "
            "ORDER BY chain_id"
        ).fetchall()
        return [str(r["chain_id"]) for r in rows]

    def decision_counts(self) -> Dict[str, int]:
        """
        Return a mapping of ``decision -> count`` across every TC in
        the archive. Keys follow the canonical decision vocabulary.
        """
        rows = self._conn.execute(
            "SELECT decision, COUNT(*) AS n FROM trust_certificates "
            "GROUP BY decision"
        ).fetchall()
        return {str(r["decision"]): int(r["n"]) for r in rows}

    def tis_distribution(self) -> Dict[str, Any]:
        """
        Return a compact distribution snapshot for TIS_current across
        every TC in the archive.

        Returns the count, mean, min, max, and a simple 4-bucket
        histogram that mirrors the decision zones at r3:

            stop_zone:     [0.00, 0.70)   — below escalate
            review_zone:   [0.70, 0.85)   — hold zone
            allow_zone:    [0.85, 1.00]   — allow
            invalidated:   TIS_current == 0.0000 with lifecycle_state
                           in ('blocked', 'invalidated')

        ``invalidated`` and ``stop_zone`` overlap intentionally — the
        same row counts in both if it is a hard stop. The fields are
        for dashboards, not for governance arithmetic.
        """
        rows = self._conn.execute(
            "SELECT tis_current, lifecycle_state FROM trust_certificates"
        ).fetchall()
        if not rows:
            return {
                "count": 0,
                "mean": 0.0,
                "min": 0.0,
                "max": 0.0,
                "histogram": {
                    "stop_zone": 0,
                    "review_zone": 0,
                    "allow_zone": 0,
                    "invalidated": 0,
                },
            }

        values = [float(r["tis_current"]) for r in rows]
        histogram = {
            "stop_zone": 0,
            "review_zone": 0,
            "allow_zone": 0,
            "invalidated": 0,
        }
        for r in rows:
            v = float(r["tis_current"])
            state = str(r["lifecycle_state"])
            if v < 0.70:
                histogram["stop_zone"] += 1
            elif v < 0.85:
                histogram["review_zone"] += 1
            else:
                histogram["allow_zone"] += 1
            if v == 0.0 and state in ("blocked", "invalidated"):
                histogram["invalidated"] += 1

        return {
            "count": len(values),
            "mean": round(sum(values) / len(values), 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "histogram": histogram,
        }

    def gate_failure_rate(self) -> float:
        """
        Fraction of TCs whose gate did NOT pass.

        Reads the denormalized decision column: Stop / Hold with a
        gate-path blocking_reason imply gate_passed=False. For a
        simpler and more reliable metric, we read the decision field:
        any TC with decision in {Stop, Hold, Escalate} counts as a
        gate-adjacent failure; Allow + Observe count as passes.

        Phase 3 will replace this with a gate_passed column query.

        Returns 0.0 if the archive is empty.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM trust_certificates"
        ).fetchone()
        total = int(row["n"])
        if total == 0:
            return 0.0
        row_fail = self._conn.execute(
            "SELECT COUNT(*) AS n FROM trust_certificates "
            "WHERE decision IN ('Stop', 'Hold', 'Escalate')"
        ).fetchone()
        return round(int(row_fail["n"]) / total, 4)

    def governance_integrity_score(self) -> float:
        """
        System-level governance integrity metric in [0, 1].

        Phase 2 formula:

            integrity = (pct_allow_or_observe * 0.4)
                      + (chain_intact ? 0.4 : 0.0)
                      + 0.2   # base infrastructure health (stub)

        The 0.2 base represents fail-safe readiness, identity provider
        reachability, and TC-write path liveness — all stubbed at 1.0
        in Phase 2. Phase 3 will make each one a real probe.
        """
        counts = self.decision_counts()
        total = sum(counts.values())
        allow_or_observe = counts.get("Allow", 0) + counts.get("Observe", 0)
        pct_clean = (allow_or_observe / total) if total else 1.0
        chain_bonus = 0.4 if self.all_chains_verify() else 0.0
        return round(pct_clean * 0.4 + chain_bonus + 0.2, 4)

    def dimension_means(self) -> Dict[str, float]:
        """
        Return mean score for each dimension (B, A, C, U) across all
        TCs in the archive. Parsed from content_json.
        """
        rows = self._conn.execute(
            "SELECT content_json FROM trust_certificates"
        ).fetchall()
        if not rows:
            return {"B": 0.0, "A": 0.0, "C": 0.0, "K": 0.0}

        sums: Dict[str, float] = {"B": 0.0, "A": 0.0, "C": 0.0, "K": 0.0}
        n = 0
        for r in rows:
            tc = _tc_from_json(r["content_json"])
            cs = tc.component_scores
            if cs:
                for dim in ("B", "A", "C", "K"):
                    sums[dim] += cs.get(dim, 0.0)
                n += 1
        if n == 0:
            return sums
        return {dim: round(v / n, 4) for dim, v in sums.items()}

    def dominant_failure_dimension(self) -> Optional[str]:
        """
        Return the dimension with the lowest mean score across all TCs,
        i.e. the dimension most responsible for gate failures. Returns
        None if no TCs exist.
        """
        means = self.dimension_means()
        if not any(means.values()):
            return None
        return min(means, key=means.get)  # type: ignore[arg-type]

    def all_chains_verify(self) -> bool:
        """Return True iff every chain in the archive verifies."""
        for chain_id in self.list_chain_ids():
            if not self.verify_chain(chain_id):
                return False
        return True

    # ---- Verification --------------------------------------------------- #

    def verify_chain(self, chain_id: str) -> bool:
        """
        Walk the chain and verify:

            a. each stored tc_hash equals compute_tc_hash(stored content)
            b. each previous_tc_hash equals the prior row's tc_hash
            c. chain_sequence is 1, 2, 3, ... with no gaps

        Returns True only if all three conditions hold for every TC in
        the chain. Returns True on an empty chain (nothing to verify).
        """
        tcs = self.list_chain(chain_id)
        if not tcs:
            return True

        prev_hash: Optional[str] = None
        expected_seq = 1
        for tc in tcs:
            ai = tc.audit_integrity
            if ai is None:
                return False

            # (a) content hash stable under recompute
            if compute_tc_hash(tc.to_dict()) != ai.tc_hash:
                return False

            # (b) previous_tc_hash linkage
            if expected_seq == 1:
                if ai.previous_tc_hash is not None:
                    return False
            else:
                if ai.previous_tc_hash != prev_hash:
                    return False

            # (c) monotonic sequence
            if ai.chain_sequence != expected_seq:
                return False

            prev_hash = ai.tc_hash
            expected_seq += 1

        return True

    # ---- Internal helpers ----------------------------------------------- #

    def _last_in_chain_locked(
        self,
        conn: sqlite3.Connection,
        chain_id: str,
    ) -> Optional[sqlite3.Row]:
        """
        Return the row with the highest chain_sequence for chain_id,
        or None if the chain is empty. Called inside a transaction.
        """
        return conn.execute(
            "SELECT tc_hash, chain_sequence "
            "FROM trust_certificates "
            "WHERE chain_id = ? "
            "ORDER BY chain_sequence DESC LIMIT 1",
            (chain_id,),
        ).fetchone()

    def _insert_tc_locked(
        self,
        conn: sqlite3.Connection,
        tc: TrustCertificate,
    ) -> None:
        """
        Insert one TC into trust_certificates. Must be called inside a
        transaction. Translates schema-trigger errors into typed
        :class:`AppendOnlyViolation` and schema-sequence errors into
        :class:`ChainSequenceError`.
        """
        ai = tc.audit_integrity
        assert ai is not None

        content_json = tc.to_json(indent=None)

        params = (
            tc.certificate_id,
            tc.subject_id,
            tc.subject_type,
            tc.domain,
            tc.risk_tier,
            tc.action_class,
            tc.policy_set_id,
            tc.decision,
            tc.lifecycle_state,
            tc.invalidation_status,
            float(tc.tis_raw),
            float(tc.tis_adjusted),
            float(tc.tis_current),
            tc.evaluation_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            tc.valid_until.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ai.tc_hash,
            ai.previous_tc_hash,
            ai.chain_id,
            int(ai.chain_sequence),
            ai.hash_algorithm,
            None,  # amended_tc_id — set by amend() path only
            content_json,
        )

        try:
            conn.execute(
                """
                INSERT INTO trust_certificates (
                    certificate_id, subject_id, subject_type, domain,
                    risk_tier, action_class, policy_set_id,
                    decision, lifecycle_state, invalidation_status,
                    tis_raw, tis_adjusted, tis_current,
                    evaluation_timestamp, valid_until,
                    tc_hash, previous_tc_hash,
                    chain_id, chain_sequence, hash_algorithm,
                    amended_tc_id, content_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                params,
            )

            # Record the initial lifecycle event synthesized from the TC.
            if tc.state_transition_history:
                initial = tc.state_transition_history[0]
                conn.execute(
                    "INSERT INTO lifecycle_events "
                    "(certificate_id, from_state, to_state, reason, occurred_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        tc.certificate_id,
                        initial.get("from"),
                        initial.get("to"),
                        initial.get("reason"),
                        initial.get("timestamp")
                            or tc.evaluation_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    ),
                )

        except sqlite3.IntegrityError as e:
            translated = translate_append_only_error(e)
            if isinstance(translated, AppendOnlyViolation):
                raise translated from e
            msg = str(e)
            # (chain_id, chain_sequence) uniqueness violation — we tried
            # to insert a TC whose chain position is already occupied.
            # This is a governance error, not an append-only error.
            if "chain_id" in msg or "chain_sequence" in msg or "UNIQUE" in msg:
                raise ChainSequenceError(
                    f"Chain linkage conflict: {e}"
                ) from e
            raise


# --------------------------------------------------------------------------- #
# JSON round-trip helpers                                                      #
# --------------------------------------------------------------------------- #
#
# The TC is serialized via to_dict() / to_json() in Phase 1. Re-hydrating
# it means reversing that: parse the JSON, then rebuild the dataclass
# tree. We only need enough fidelity to satisfy verify_chain() and the
# fields consumed by the Phase 2 API layer, not full equality with the
# original TC — the content_json itself is the canonical form.

from datetime import datetime, timezone  # noqa: E402  (kept near usage site)

from tcs.trust_certificate import (  # noqa: E402
    IdentityBinding,
    GovernanceStatus,
    OverrideRecord,
)


def _parse_iso8601(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    # strip trailing Z if present
    stripped = s[:-1] if s.endswith("Z") else s
    return datetime.fromisoformat(stripped)


def _build_audit_integrity(d: Optional[Dict[str, Any]]) -> Optional[AuditIntegrity]:
    if d is None:
        return None
    return AuditIntegrity(
        tc_hash=d["tc_hash"],
        previous_tc_hash=d.get("previous_tc_hash"),
        chain_sequence=int(d["chain_sequence"]),
        chain_id=d["chain_id"],
        hash_algorithm=d.get("hash_algorithm", "sha256"),
        integrity_verified=bool(d.get("integrity_verified", True)),
        issued_by=d.get("issued_by", "tcs-reference-impl-v0.1"),
    )


def _build_identity_binding(d: Optional[Dict[str, Any]]) -> Optional[IdentityBinding]:
    if d is None:
        return None
    return IdentityBinding(
        requesting_identity=d["requesting_identity"],
        identity_type=d["identity_type"],
        role=d["role"],
        authorization_tier=d["authorization_tier"],
        identity_confidence=float(d["identity_confidence"]),
        identity_verified=bool(d["identity_verified"]),
        authentication_method=d["authentication_method"],
        requesting_session_id=d["requesting_session_id"],
    )


def _build_governance_status(d: Optional[Dict[str, Any]]) -> Optional[GovernanceStatus]:
    if d is None:
        return None
    return GovernanceStatus(
        governance_status=d["governance_status"],
        evaluation_completeness_score=float(d["evaluation_completeness_score"]),
        components_evaluated=list(d.get("components_evaluated", [])),
        components_skipped=list(d.get("components_skipped", [])),
        skip_reasons=dict(d.get("skip_reasons", {})),
        fail_safe_applied=bool(d.get("fail_safe_applied", False)),
        fail_safe_type=d.get("fail_safe_type"),
        governance_integrity_score=float(d.get("governance_integrity_score", 1.0)),
    )


def _build_override_record(d: Optional[Dict[str, Any]]) -> Optional[OverrideRecord]:
    if d is None:
        return None
    return OverrideRecord(
        override_invoked=bool(d.get("override_invoked", False)),
        original_decision=d.get("original_decision"),
        override_decision=d.get("override_decision"),
        override_actor=d.get("override_actor"),
        override_actor_role=d.get("override_actor_role"),
        override_reason=d.get("override_reason"),
        override_type=d.get("override_type"),
        policy_exception_id=d.get("policy_exception_id"),
        regulatory_basis=d.get("regulatory_basis"),
        co_authorizer=d.get("co_authorizer"),
        post_override_review_required=bool(
            d.get("post_override_review_required", False)
        ),
        post_override_review_deadline=d.get("post_override_review_deadline"),
        post_override_review_completed=bool(
            d.get("post_override_review_completed", False)
        ),
        override_creates_tc_amendment=bool(
            d.get("override_creates_tc_amendment", False)
        ),
    )


def _tc_from_json(content_json: str) -> TrustCertificate:
    """
    Rebuild a TrustCertificate from its stored content_json.

    This round-trip fidelity is sufficient for:
        - verify_chain() (needs audit_integrity + content hash)
        - get() returning a TC that to_dict() reproduces identically
    """
    d = json.loads(content_json)

    return TrustCertificate(
        # Identity
        certificate_id=d["certificate_id"],
        subject_id=d["subject_id"],
        subject_type=d["subject_type"],
        domain=d["domain"],
        risk_tier=d["risk_tier"],
        action_class=d["action_class"],
        policy_severity=d["policy_severity"],
        checkpoint_id=d["checkpoint_id"],
        gca_context_id=d["gca_context_id"],
        policy_set_id=d["policy_set_id"],

        # Score. s_base / s_adjusted fall back to tis_raw / tis_adjusted
        # for legacy archived TCs written before the white-paper-aligned
        # naming split (where tis_raw was the gate-INDEPENDENT composite,
        # i.e. semantically s_base). New TCs always carry s_base directly.
        s_base=float(d.get("s_base", d.get("tis_raw", 0.0))),
        s_adjusted=float(d.get("s_adjusted", d.get("tis_adjusted", 0.0))),
        tis_raw=float(d["tis_raw"]),
        tis_adjusted=float(d["tis_adjusted"]),
        tis_current=float(d["tis_current"]),
        component_scores=dict(d["component_scores"]),
        component_weights=dict(d["component_weights"]),
        penalty_aggregate=float(d["penalty_aggregate"]),
        penalty_breakdown=dict(d["penalty_breakdown"]),
        failing_dimension_subfactors=dict(d.get("failing_dimension_subfactors", {})),

        # Gate
        gate_set=list(d["gate_set"]),
        thresholds=dict(d["thresholds"]),
        gate_results=dict(d["gate_results"]),
        gate_passed=bool(d["gate_passed"]),
        blocking_reason=d.get("blocking_reason"),
        failure_mode=d.get("failure_mode"),

        # Decision
        decision=d["decision"],
        requires_human_review=bool(d["requires_human_review"]),
        escalation_routed_to=list(d.get("escalation_routed_to", [])),

        # Provenance
        source_references=list(d.get("source_references", [])),
        retrieval_ids=list(d.get("retrieval_ids", [])),
        chain_of_custody_id=d["chain_of_custody_id"],
        audit_log_id=d["audit_log_id"],
        integration_boundary_gaps=int(d["integration_boundary_gaps"]),

        # Temporal
        evaluation_timestamp=_parse_iso8601(d["evaluation_timestamp"]),
        valid_until=_parse_iso8601(d["valid_until"]),
        decay_rate=float(d["decay_rate"]),
        recompute_required=bool(d["recompute_required"]),
        invalidation_triggers=list(d.get("invalidation_triggers", [])),
        last_invalidation_event=dict(d.get("last_invalidation_event", {})),
        invalidation_status=d["invalidation_status"],

        # Explanation
        explanation_summary=d["explanation_summary"],
        key_factors=list(d.get("key_factors", [])),
        key_concerns=list(d.get("key_concerns", [])),
        regulatory_explanation_level=d["regulatory_explanation_level"],
        regulatory_mapping=list(d.get("regulatory_mapping", [])),

        # Lifecycle
        lifecycle_state=d["lifecycle_state"],
        state_transition_history=list(d.get("state_transition_history", [])),
        recomputed_from_certificate_id=d.get("recomputed_from_certificate_id"),
        superseded_by_certificate_id=d.get("superseded_by_certificate_id"),
        archived=bool(d.get("archived", False)),

        # MCP Extensions
        mcp_server_id=d.get("mcp_server_id"),
        scope_attestation=dict(d.get("scope_attestation", {})),

        # CT audit fields
        connection_type=d.get("connection_type"),
        connection_type_modifier_id=d.get("connection_type_modifier_id"),
        resolved_policy_profile_id=d.get("resolved_policy_profile_id"),
        chain_depth=int(d.get("chain_depth", 0)),
        chain_u_scores=[float(x) for x in d.get("chain_u_scores", [])],

        # Standards Composer audit (Slice 4)
        composer_metadata=(
            dict(d["composer_metadata"])
            if isinstance(d.get("composer_metadata"), dict) else None
        ),

        # Governance Risk Rule audit (Slice 4.5).
        # Stored as a JSON list; round-trip preserves None vs [] semantics
        # (None = classifier did not run; [] = ran with no matches). The
        # ``.get(..., None)`` default plus the explicit None check below
        # is the backward-compat fallback for TCs written before this
        # field existed.
        governance_rule_matches=(
            [dict(m) for m in d["governance_rule_matches"]
             if isinstance(m, dict)]
            if isinstance(d.get("governance_rule_matches"), list)
            else None
        ),

        # TEL layers
        identity_binding=_build_identity_binding(d.get("identity_binding")),
        governance_status=_build_governance_status(d.get("governance_status")),
        audit_integrity=_build_audit_integrity(d.get("audit_integrity")),
        override_record=_build_override_record(d.get("override_record")),
    )


# --------------------------------------------------------------------------- #
# Policy adaptation persistence (Phase 3 Step 4)                               #
# --------------------------------------------------------------------------- #

# These methods live outside the CertificateStore class but use the same
# connection. They are added as module-level functions that accept a store
# to keep CertificateStore focused on TC persistence. We attach them as
# methods below for API convenience.

def _insert_adaptation(
    self: "CertificateStore",
    record_id: str,
    triggered_by: str,
    profile_id: str,
    parameter_changes: Dict[str, Any],
    evidence: Dict[str, Any],
    rollback_available_until: Optional[str] = None,
) -> None:
    """Insert a new policy adaptation record (pending status)."""
    self._conn.execute(
        """INSERT INTO policy_adaptations (
            record_id, triggered_by, risk_tolerance_profile_id,
            parameter_changes_json, evidence_json,
            approval_status, rollback_available_until
        ) VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
        (
            record_id,
            triggered_by,
            profile_id,
            json.dumps(parameter_changes),
            json.dumps(evidence),
            rollback_available_until,
        ),
    )


def _get_adaptation(
    self: "CertificateStore", record_id: str
) -> Optional[Dict[str, Any]]:
    """Fetch a single adaptation record by record_id."""
    row = self._conn.execute(
        "SELECT * FROM policy_adaptations WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    return dict(row) if row else None


def _update_adaptation_status(
    self: "CertificateStore",
    record_id: str,
    status: str,
    approver: Optional[str] = None,
    applied_at: Optional[str] = None,
) -> bool:
    """
    Update the approval_status of an adaptation record.
    Returns True if the record was found and updated.
    """
    from datetime import datetime, timezone

    row = self._conn.execute(
        "SELECT id FROM policy_adaptations WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    if row is None:
        return False

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    self._conn.execute(
        """UPDATE policy_adaptations
           SET approval_status = ?,
               approver_identity = COALESCE(?, approver_identity),
               approval_timestamp = CASE WHEN ? IN ('approved','rejected') THEN ? ELSE approval_timestamp END,
               applied_at = COALESCE(?, applied_at)
           WHERE record_id = ?""",
        (status, approver, status, ts, applied_at, record_id),
    )
    return True


def _list_adaptations(
    self: "CertificateStore",
    profile_id: Optional[str] = None,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List adaptation records with optional filters."""
    query = "SELECT * FROM policy_adaptations WHERE 1=1"
    params: List[Any] = []
    if profile_id:
        query += " AND risk_tolerance_profile_id = ?"
        params.append(profile_id)
    if status:
        query += " AND approval_status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC"
    rows = self._conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# Attach as methods on CertificateStore
CertificateStore.insert_adaptation = _insert_adaptation
CertificateStore.get_adaptation = _get_adaptation
CertificateStore.update_adaptation_status = _update_adaptation_status
CertificateStore.list_adaptations = _list_adaptations


# --------------------------------------------------------------------------- #
# Recovery incident persistence (Phase 3 Step 5)                               #
# --------------------------------------------------------------------------- #

def _insert_recovery_incident(
    self: "CertificateStore",
    incident_id: str,
    trigger_d_trust: float,
    trigger_context: str,
    trigger_evidence: Dict[str, Any],
) -> None:
    """Insert a new recovery incident (containment phase, active status)."""
    import json as _json
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    phase_history = [{"phase": "containment", "entered_at": now}]
    self._conn.execute(
        """INSERT INTO recovery_incidents (
            incident_id, current_phase, status,
            trigger_d_trust, trigger_context, trigger_evidence_json,
            phase_history_json, activated_at, updated_at
        ) VALUES (?, 'containment', 'active', ?, ?, ?, ?, ?, ?)""",
        (
            incident_id, trigger_d_trust, trigger_context,
            _json.dumps(trigger_evidence),
            _json.dumps(phase_history), now, now,
        ),
    )


def _get_recovery_incident(
    self: "CertificateStore", incident_id: str
) -> Optional[Dict[str, Any]]:
    """Fetch a single recovery incident by incident_id."""
    row = self._conn.execute(
        "SELECT * FROM recovery_incidents WHERE incident_id = ?",
        (incident_id,),
    ).fetchone()
    return dict(row) if row else None


def _get_active_recovery(self: "CertificateStore") -> Optional[Dict[str, Any]]:
    """Fetch the currently active recovery incident, if any."""
    row = self._conn.execute(
        "SELECT * FROM recovery_incidents WHERE status = 'active' "
        "ORDER BY activated_at DESC LIMIT 1",
    ).fetchone()
    return dict(row) if row else None


def _update_recovery_incident(
    self: "CertificateStore",
    incident_id: str,
    **kwargs: Any,
) -> bool:
    """Update mutable fields on a recovery incident."""
    import json as _json
    allowed = {
        "current_phase", "status", "diagnostic_json", "remediation_json",
        "s_recovery", "phase_history_json", "completed_at", "updated_at",
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return False
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    updates.setdefault("updated_at", now)

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [incident_id]
    self._conn.execute(
        f"UPDATE recovery_incidents SET {set_clause} WHERE incident_id = ?",
        values,
    )
    return True


def _list_recovery_incidents(
    self: "CertificateStore", status: Optional[str] = None
) -> List[Dict[str, Any]]:
    """List recovery incidents, optionally filtered by status."""
    if status:
        rows = self._conn.execute(
            "SELECT * FROM recovery_incidents WHERE status = ? "
            "ORDER BY activated_at DESC", (status,),
        ).fetchall()
    else:
        rows = self._conn.execute(
            "SELECT * FROM recovery_incidents ORDER BY activated_at DESC",
        ).fetchall()
    return [dict(r) for r in rows]


CertificateStore.insert_recovery_incident = _insert_recovery_incident
CertificateStore.get_recovery_incident = _get_recovery_incident
CertificateStore.get_active_recovery = _get_active_recovery
CertificateStore.update_recovery_incident = _update_recovery_incident
CertificateStore.list_recovery_incidents = _list_recovery_incidents


# --------------------------------------------------------------------------- #
# Control Plane Observability (Phase 4 Step 5)                                 #
# --------------------------------------------------------------------------- #

def _timeseries_buckets(
    self: "CertificateStore",
    window_hours: float = 1.0,
    bucket_minutes: float = 1.0,
) -> list:
    """
    Return time-bucketed decision counts and mean TIS for dashboard
    timeseries charts.

    Each bucket is a dict:
        {"t": iso8601, "allow_count": int, "hold_count": int,
         "stop_count": int, "observe_count": int, "escalate_count": int,
         "mean_tis": float}
    """
    from datetime import timedelta

    rows = self.query_window(window_hours)
    if not rows:
        return []

    bucket_seconds = bucket_minutes * 60.0
    buckets: Dict[str, Dict[str, Any]] = {}

    for r in rows:
        ts_str = r["evaluation_timestamp"]
        ts = _parse_iso8601(ts_str)
        if ts is None:
            continue
        # Floor to bucket boundary
        epoch = ts.timestamp()
        floored_epoch = (epoch // bucket_seconds) * bucket_seconds
        bucket_key = datetime.fromtimestamp(floored_epoch, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        if bucket_key not in buckets:
            buckets[bucket_key] = {
                "t": bucket_key,
                "allow_count": 0,
                "hold_count": 0,
                "stop_count": 0,
                "observe_count": 0,
                "escalate_count": 0,
                "_tis_sum": 0.0,
                "_tis_n": 0,
            }

        b = buckets[bucket_key]
        decision = r["decision"]
        if decision == "Allow":
            b["allow_count"] += 1
        elif decision == "Hold":
            b["hold_count"] += 1
        elif decision == "Stop":
            b["stop_count"] += 1
        elif decision == "Observe":
            b["observe_count"] += 1
        elif decision == "Escalate":
            b["escalate_count"] += 1

        b["_tis_sum"] += float(r["tis_current"])
        b["_tis_n"] += 1

    result = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        mean_tis = round(b["_tis_sum"] / b["_tis_n"], 4) if b["_tis_n"] > 0 else 0.0
        result.append({
            "t": b["t"],
            "allow_count": b["allow_count"],
            "hold_count": b["hold_count"],
            "stop_count": b["stop_count"],
            "observe_count": b["observe_count"],
            "escalate_count": b["escalate_count"],
            "mean_tis": mean_tis,
        })
    return result


def _gate_failure_details(
    self: "CertificateStore",
    window_hours: float = 24.0,
) -> dict:
    """
    Return gate failure breakdown by dimension and by profile.

    Returns:
        {"total": int,
         "by_dimension": {"B": int, "A": int, "C": int, "K": int},
         "by_profile": {"profile-id": int, ...}}
    """
    rows = self.query_window(window_hours)
    by_dimension: Dict[str, int] = {"B": 0, "A": 0, "C": 0, "K": 0}
    by_profile: Dict[str, int] = {}
    total = 0

    for r in rows:
        d = json.loads(r["content_json"])
        if d.get("gate_passed", True):
            continue
        total += 1

        gate_results = d.get("gate_results", {})
        for dim in ("B", "A", "C", "K"):
            if gate_results.get(dim) == "fail":
                by_dimension[dim] += 1

        profile_id = d.get("policy_set_id", "unknown")
        by_profile[profile_id] = by_profile.get(profile_id, 0) + 1

    return {
        "total": total,
        "by_dimension": by_dimension,
        "by_profile": by_profile,
    }


def _attribution_gap_details(
    self: "CertificateStore",
    window_hours: float = 24.0,
) -> dict:
    """
    Return attribution gap metrics and trend.

    Returns:
        {"total_gaps": int, "mean_gaps_per_eval": float,
         "trend": [{"t": iso8601, "n_gaps": int}]}
    """
    rows = self.query_window(window_hours)
    total_gaps = 0
    trend: List[Dict[str, Any]] = []

    for r in rows:
        d = json.loads(r["content_json"])
        n_gaps = int(d.get("integration_boundary_gaps", 0))
        total_gaps += n_gaps
        trend.append({
            "t": r["evaluation_timestamp"],
            "n_gaps": n_gaps,
        })

    n_evals = len(rows)
    mean_gaps = round(total_gaps / n_evals, 4) if n_evals > 0 else 0.0

    return {
        "total_gaps": total_gaps,
        "mean_gaps_per_eval": mean_gaps,
        "trend": trend,
    }


def _chain_summary(
    self: "CertificateStore",
    chain_id: str,
) -> dict:
    """
    Return summary for a specific chain including length, timestamps,
    decision counts, and verification status.

    Returns:
        {"chain_id": str, "length": int, "first_at": iso8601,
         "last_at": iso8601, "verified": bool,
         "decisions": {"Allow": int, ...}}
    """
    rows = self._conn.execute(
        "SELECT decision, evaluation_timestamp FROM trust_certificates "
        "WHERE chain_id = ? ORDER BY chain_sequence ASC",
        (chain_id,),
    ).fetchall()

    if not rows:
        return {
            "chain_id": chain_id,
            "length": 0,
            "first_at": None,
            "last_at": None,
            "verified": True,
            "decisions": {},
        }

    decisions: Dict[str, int] = {}
    for r in rows:
        dec = str(r["decision"])
        decisions[dec] = decisions.get(dec, 0) + 1

    return {
        "chain_id": chain_id,
        "length": len(rows),
        "first_at": rows[0]["evaluation_timestamp"],
        "last_at": rows[-1]["evaluation_timestamp"],
        "verified": self.verify_chain(chain_id),
        "decisions": decisions,
    }


def _telemetry_stream(
    self: "CertificateStore",
    window_hours: float = 1.0,
    limit: int = 100,
) -> list:
    """
    Return per-evaluation telemetry records for real-time charting.

    Each record contains: timestamp, TIS scores (raw/adj/current),
    per-dimension scores (B/A/C/K), penalty breakdown, decision,
    gate pass/fail, and governance latency.

    This powers the Telemetry view — dimension score trends,
    K calibration sparkline, penalty pressure over time.
    """
    rows = self.query_window(window_hours)
    if not rows:
        return []

    records = []
    for r in rows[-limit:]:  # most recent N
        d = json.loads(r["content_json"])
        cs = d.get("component_scores", {})
        pb = d.get("penalty_breakdown", {})
        records.append({
            "t": r["evaluation_timestamp"],
            "certificate_id": d.get("certificate_id", ""),
            "subject_id": d.get("subject_id", ""),
            "decision": r["decision"],
            "tis_raw": round(float(d.get("tis_raw", 0.0)), 4),
            "tis_adjusted": round(float(d.get("tis_adjusted", 0.0)), 4),
            "tis_current": float(r["tis_current"]),
            "B": round(cs.get("B", 0.0), 4),
            "A": round(cs.get("A", 0.0), 4),
            "C": round(cs.get("C", 0.0), 4),
            # Read-side legacy fallback: archived TCs written before the
            # BACU -> BACK migration may have "U" instead of "K". This is
            # NOT a translation layer — new writes always use K end-to-end.
            "K": round(cs.get("K", cs.get("U", 0.0)), 4),
            "gate_passed": d.get("gate_passed", True),
            "P_cb": round(pb.get("P_cb", 0.0), 4),
            "P_d": round(pb.get("P_d", 0.0), 4),
            "P_n": round(pb.get("P_n", 0.0), 4),
            "P_h": round(pb.get("P_h", 0.0), 4),
            "P_ps": round(pb.get("P_ps", 0.0), 4),
            "penalty_aggregate": round(float(d.get("penalty_aggregate", 0.0)), 4),
            "governance_ms": d.get("governance_ms"),
            "profile_id": d.get("policy_set_id", ""),
        })
    return records


CertificateStore.timeseries_buckets = _timeseries_buckets
CertificateStore.gate_failure_details = _gate_failure_details
CertificateStore.attribution_gap_details = _attribution_gap_details
CertificateStore.chain_summary = _chain_summary
CertificateStore.telemetry_stream = _telemetry_stream
