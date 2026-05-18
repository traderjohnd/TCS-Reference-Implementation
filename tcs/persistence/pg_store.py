"""
tcs.persistence.pg_store
========================

PostgreSQL-backed persistence for Trust Certificates.

Implements the same public API as :class:`CertificateStore` (the SQLite
store) so the two are interchangeable at the ``create_app()`` level.

Usage::

    from tcs.persistence.pg_store import PostgresCertificateStore

    store = PostgresCertificateStore(
        host="localhost", port=5432,
        database="tcs", user="tcs", password="tcs_dev",
    )
    store.run_migrations()  # idempotent — CREATE IF NOT EXISTS
    issued_tc = store.issue(tc)
    loaded_tc = store.get(issued_tc.certificate_id)
    ok = store.verify_chain(chain_id)

Requires ``psycopg[binary]`` (listed in ``requirements-pg.txt``).
When psycopg is not installed, importing this module raises ImportError
at import time — the rest of TCS continues to work on SQLite.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import psycopg
from psycopg.rows import dict_row

from tcs.trust_certificate import (
    AuditIntegrity,
    TrustCertificate,
    compute_tc_hash,
)
from tcs.persistence.certificate_store import (
    ChainSequenceError,
    CertificateNotFoundError,
    _tc_from_json,
)


# --------------------------------------------------------------------------- #
# Migrations                                                                   #
# --------------------------------------------------------------------------- #

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _load_migration(name: str) -> str:
    return (_MIGRATIONS_DIR / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# PostgresCertificateStore                                                     #
# --------------------------------------------------------------------------- #

class PostgresCertificateStore:
    """
    PostgreSQL-backed TC archive with the same public API as the SQLite
    :class:`CertificateStore`.

    The store acquires a connection from the pool for each operation (or
    uses an explicit transaction block). This is safe for concurrent
    FastAPI worker threads.

    Parameters
    ----------
    host, port, database, user, password
        PostgreSQL connection parameters. Can also be set via env vars
        ``TCS_PG_HOST``, ``TCS_PG_PORT``, etc.
    dsn
        Full libpq connection string. If provided, the individual
        host/port/database/user/password params are ignored.
    conn
        Pre-existing psycopg connection (for testing). If provided, the
        store uses it directly and does NOT close it on ``close()``.
    """

    def __init__(
        self,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        database: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        dsn: Optional[str] = None,
        conn: Optional[psycopg.Connection] = None,
    ) -> None:
        if conn is not None:
            self._conn = conn
            self._owns_conn = False
        else:
            conninfo = dsn or self._build_dsn(host, port, database, user, password)
            self._conn = psycopg.connect(conninfo, row_factory=dict_row, autocommit=True)
            self._owns_conn = True

    @staticmethod
    def _build_dsn(
        host: Optional[str],
        port: Optional[int],
        database: Optional[str],
        user: Optional[str],
        password: Optional[str],
    ) -> str:
        h = host or os.environ.get("TCS_PG_HOST", "localhost")
        p = port or int(os.environ.get("TCS_PG_PORT", "5432"))
        d = database or os.environ.get("TCS_PG_DATABASE", "tcs")
        u = user or os.environ.get("TCS_PG_USER", "tcs")
        pw = password or os.environ.get("TCS_PG_PASSWORD", "tcs_dev")
        return f"host={h} port={p} dbname={d} user={u} password={pw}"

    # ---- Context manager --------------------------------------------------- #

    def __enter__(self) -> "PostgresCertificateStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_conn and self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None  # type: ignore[assignment]

    # ---- Migrations -------------------------------------------------------- #

    def run_migrations(self) -> None:
        """
        Apply all SQL migration files in order. Idempotent — every
        statement uses IF NOT EXISTS or DO $$ ... EXCEPTION blocks.
        """
        for name in sorted(os.listdir(_MIGRATIONS_DIR)):
            if name.endswith(".sql"):
                sql = _load_migration(name)
                self._conn.execute(sql)

    # ---- Transaction helper ------------------------------------------------ #

    @contextmanager
    def _transaction(self) -> Iterator[psycopg.Connection]:
        """Explicit BEGIN/COMMIT block with ROLLBACK on failure."""
        with self._conn.transaction():
            yield self._conn

    # ---- Core: issue ------------------------------------------------------- #

    def issue(self, tc: TrustCertificate) -> TrustCertificate:
        """
        Assign chain linkage, (re)compute the hash, and append the TC.
        Same semantics as the SQLite CertificateStore.issue().
        """
        if tc.audit_integrity is None:
            raise ChainSequenceError(
                "Cannot issue a TC with audit_integrity=None; "
                "call generate_certificate() first"
            )

        with self._transaction() as conn:
            chain_id = tc.audit_integrity.chain_id
            last = self._last_in_chain(conn, chain_id)

            if last is None:
                new_sequence = 1
                new_previous_hash: Optional[str] = None
            else:
                new_sequence = int(last["chain_sequence"]) + 1
                new_previous_hash = str(last["tc_hash"])

            new_audit = AuditIntegrity(
                tc_hash=tc.audit_integrity.tc_hash,
                previous_tc_hash=new_previous_hash,
                chain_sequence=new_sequence,
                chain_id=chain_id,
                hash_algorithm=tc.audit_integrity.hash_algorithm,
                integrity_verified=True,
                issued_by=tc.audit_integrity.issued_by,
            )

            issued_tc = dataclass_replace(tc, audit_integrity=new_audit)
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

            self._insert_tc(conn, issued_tc)
            return issued_tc

    # ---- Retrieval --------------------------------------------------------- #

    def get(self, certificate_id: str) -> TrustCertificate:
        """Re-hydrate a TC from its stored content_json."""
        row = self._conn.execute(
            "SELECT content_json FROM trust_certificates "
            "WHERE certificate_id = %s",
            (certificate_id,),
        ).fetchone()
        if row is None:
            raise CertificateNotFoundError(
                f"No certificate with certificate_id={certificate_id!r}"
            )
        return _tc_from_json(row["content_json"])

    def list_chain(self, chain_id: str) -> List[TrustCertificate]:
        """Return every TC in a chain, ordered by chain_sequence."""
        rows = self._conn.execute(
            "SELECT content_json FROM trust_certificates "
            "WHERE chain_id = %s ORDER BY chain_sequence ASC",
            (chain_id,),
        ).fetchall()
        return [_tc_from_json(r["content_json"]) for r in rows]

    def count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM trust_certificates"
        ).fetchone()
        return int(row["n"])

    def list_recent(self, limit: int = 20) -> List[TrustCertificate]:
        rows = self._conn.execute(
            "SELECT content_json FROM trust_certificates "
            "ORDER BY evaluation_timestamp DESC, chain_sequence DESC "
            "LIMIT %s",
            (int(limit),),
        ).fetchall()
        return [_tc_from_json(r["content_json"]) for r in rows]

    # ---- Chain IDs --------------------------------------------------------- #

    def list_chain_ids(self) -> List[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT chain_id FROM trust_certificates ORDER BY chain_id"
        ).fetchall()
        return [str(r["chain_id"]) for r in rows]

    # ---- Aggregation ------------------------------------------------------- #

    def decision_counts(self) -> Dict[str, int]:
        rows = self._conn.execute(
            "SELECT decision, COUNT(*) AS n FROM trust_certificates "
            "GROUP BY decision"
        ).fetchall()
        return {str(r["decision"]): int(r["n"]) for r in rows}

    def tis_distribution(self) -> Dict[str, Any]:
        rows = self._conn.execute(
            "SELECT tis_current, lifecycle_state FROM trust_certificates"
        ).fetchall()
        if not rows:
            return {
                "count": 0, "mean": 0.0, "min": 0.0, "max": 0.0,
                "histogram": {
                    "stop_zone": 0, "review_zone": 0,
                    "allow_zone": 0, "invalidated": 0,
                },
            }

        values = [float(r["tis_current"]) for r in rows]
        histogram = {"stop_zone": 0, "review_zone": 0, "allow_zone": 0, "invalidated": 0}
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
        counts = self.decision_counts()
        total = sum(counts.values())
        allow_or_observe = counts.get("Allow", 0) + counts.get("Observe", 0)
        pct_clean = (allow_or_observe / total) if total else 1.0
        chain_bonus = 0.4 if self.all_chains_verify() else 0.0
        return round(pct_clean * 0.4 + chain_bonus + 0.2, 4)

    def dimension_means(self) -> Dict[str, float]:
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
        means = self.dimension_means()
        if not any(means.values()):
            return None
        return min(means, key=means.get)  # type: ignore[arg-type]

    def all_chains_verify(self) -> bool:
        for chain_id in self.list_chain_ids():
            if not self.verify_chain(chain_id):
                return False
        return True

    # ---- Windowed queries -------------------------------------------------- #

    def _compute_window_cutoff(self, window_hours: float) -> str:
        from datetime import datetime, timezone, timedelta

        row = self._conn.execute(
            "SELECT MAX(evaluation_timestamp) AS latest FROM trust_certificates"
        ).fetchone()
        latest_ts = row["latest"] if row and row["latest"] else None

        if latest_ts is not None:
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
        self, window_hours: float, *, since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        cutoff = since if since else self._compute_window_cutoff(window_hours)
        rows = self._conn.execute(
            "SELECT tis_current, decision, evaluation_timestamp, content_json "
            "FROM trust_certificates "
            "WHERE evaluation_timestamp >= %s "
            "ORDER BY evaluation_timestamp ASC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    def query_window_by_context(
        self, window_hours: float, *, since: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        cutoff = since if since else self._compute_window_cutoff(window_hours)
        rows = self._conn.execute(
            "SELECT tis_current, decision, evaluation_timestamp, "
            "       policy_set_id, content_json "
            "FROM trust_certificates "
            "WHERE evaluation_timestamp >= %s "
            "ORDER BY evaluation_timestamp ASC",
            (cutoff,),
        ).fetchall()
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            d = dict(r)
            ctx = d["policy_set_id"]
            grouped.setdefault(ctx, []).append(d)
        return grouped

    # ---- Verification ------------------------------------------------------ #

    def verify_chain(self, chain_id: str) -> bool:
        tcs = self.list_chain(chain_id)
        if not tcs:
            return True

        prev_hash: Optional[str] = None
        expected_seq = 1
        for tc in tcs:
            ai = tc.audit_integrity
            if ai is None:
                return False
            if compute_tc_hash(tc.to_dict()) != ai.tc_hash:
                return False
            if expected_seq == 1:
                if ai.previous_tc_hash is not None:
                    return False
            else:
                if ai.previous_tc_hash != prev_hash:
                    return False
            if ai.chain_sequence != expected_seq:
                return False
            prev_hash = ai.tc_hash
            expected_seq += 1
        return True

    # ---- Internal helpers -------------------------------------------------- #

    def _last_in_chain(
        self, conn: psycopg.Connection, chain_id: str,
    ) -> Optional[Dict[str, Any]]:
        return conn.execute(
            "SELECT tc_hash, chain_sequence "
            "FROM trust_certificates "
            "WHERE chain_id = %s "
            "ORDER BY chain_sequence DESC LIMIT 1",
            (chain_id,),
        ).fetchone()

    def _insert_tc(
        self, conn: psycopg.Connection, tc: TrustCertificate,
    ) -> None:
        ai = tc.audit_integrity
        assert ai is not None

        content_json = tc.to_json(indent=None)

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
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                )
                """,
                (
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
                    None,  # amended_tc_id
                    content_json,
                ),
            )

            # Record the initial lifecycle event.
            if tc.state_transition_history:
                initial = tc.state_transition_history[0]
                conn.execute(
                    "INSERT INTO lifecycle_events "
                    "(certificate_id, from_state, to_state, reason, occurred_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        tc.certificate_id,
                        initial.get("from"),
                        initial.get("to"),
                        initial.get("reason"),
                        initial.get("timestamp")
                            or tc.evaluation_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    ),
                )

        except psycopg.errors.UniqueViolation as e:
            msg = str(e)
            if "chain_id" in msg or "chain_sequence" in msg:
                raise ChainSequenceError(f"Chain linkage conflict: {e}") from e
            raise
