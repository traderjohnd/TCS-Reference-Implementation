"""
tcs.artifacts.store
===================

Persistence layer for ResponseArtifact and GovernanceEvaluation.

Mirrors the pattern of ``tcs.persistence.certificate_store.CertificateStore``:

  - same SQLite connection / db file
  - append-only at the trigger level (see db.py)
  - ``content_json`` carries the full dataclass blob; denormalized
    columns (provider, mode, decision, ...) are indexed for fast lookup
  - ``AppendOnlyViolation`` is raised when a caller attempts UPDATE or
    DELETE on either table
  - foreign key ``governance_evaluations.artifact_id →
    response_artifacts.artifact_id`` is enforced by SQLite via PRAGMA
    foreign_keys=ON (set by ``open_connection``).

Phase 5 scope reminder: this store is the foundation for the replay
contract. Production deployments will need configurable retention,
redaction, and WORM/archival policy because raw_output may contain
PHI, PII, financial data, or confidential business content. Phase 5
keeps artifacts forever in the reference implementation; that policy
is NOT the production rule.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional, Union

from tcs.artifacts.models import GovernanceEvaluation, ResponseArtifact
from tcs.persistence.db import (
    AppendOnlyViolation,
    init_db,
    translate_append_only_error,
)


class ArtifactNotFoundError(KeyError):
    """Raised when get_artifact / get_evaluation finds no row."""


class ArtifactStore:
    """
    Append-only store for ResponseArtifact and GovernanceEvaluation.

    Either pass a ``db_path`` (a new connection is opened and the
    schema applied), or pass an existing ``conn`` (schema applied
    in-place; the caller still owns the connection lifetime). The
    typical Phase 5 deployment shares the same connection as the
    CertificateStore so all governance tables live in one db file.
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

    def __enter__(self) -> "ArtifactStore":
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
        """Explicit BEGIN/COMMIT with ROLLBACK on exception."""
        self._conn.execute("BEGIN")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ---- ResponseArtifact ---------------------------------------------- #

    def insert_artifact(self, artifact: ResponseArtifact) -> ResponseArtifact:
        """
        Persist a ResponseArtifact. Returns it unchanged (no
        store-assigned fields — IDs and timestamps are author-supplied
        or auto-derived in the dataclass).

        Raises:
            sqlite3.IntegrityError — duplicate artifact_id (UNIQUE
                constraint). This is a logic bug, not an append-only
                violation; the dataclass generates fresh UUID4 by
                default so collisions only happen if callers reuse IDs.
        """
        d = artifact.to_dict()
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO response_artifacts (
                    artifact_id, created_at, generation_mode,
                    prompt_hash, raw_output_hash, provider, model,
                    session_id, content_json
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    artifact.artifact_id,
                    d["created_at"],
                    artifact.generation_mode,
                    artifact.prompt_hash,
                    artifact.raw_output_hash,
                    artifact.provider,
                    artifact.model,
                    (artifact.generation_identity or {}).get("session_id"),
                    json.dumps(d, separators=(",", ":")),
                ),
            )
        return artifact

    def get_artifact(self, artifact_id: str) -> ResponseArtifact:
        """Hydrate one artifact by id. Raises ArtifactNotFoundError if absent."""
        row = self._conn.execute(
            "SELECT content_json FROM response_artifacts WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
        if row is None:
            raise ArtifactNotFoundError(
                f"no response_artifact with artifact_id={artifact_id!r}"
            )
        return ResponseArtifact.from_dict(json.loads(row["content_json"]))

    def list_artifacts(self, limit: int = 100) -> List[ResponseArtifact]:
        """Most-recent artifacts first."""
        rows = self._conn.execute(
            "SELECT content_json FROM response_artifacts "
            "ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [
            ResponseArtifact.from_dict(json.loads(r["content_json"]))
            for r in rows
        ]

    # ---- GovernanceEvaluation ------------------------------------------ #

    def insert_evaluation(
        self, evaluation: GovernanceEvaluation,
    ) -> GovernanceEvaluation:
        """
        Persist a GovernanceEvaluation. The FK on artifact_id is
        enforced by SQLite (PRAGMA foreign_keys=ON) — inserting an
        evaluation for an unknown artifact raises IntegrityError.
        """
        d = evaluation.to_dict()
        with self._transaction() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO governance_evaluations (
                        evaluation_id, artifact_id, created_at, mode,
                        policy_profile_id, decision, enforcement_action,
                        delivery_intervention, trust_certificate_id,
                        content_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        evaluation.evaluation_id,
                        evaluation.artifact_id,
                        d["created_at"],
                        evaluation.mode,
                        evaluation.policy_profile_id or None,
                        evaluation.decision,
                        evaluation.enforcement_action,
                        1 if evaluation.delivery_intervention else 0,
                        evaluation.trust_certificate_id,
                        json.dumps(d, separators=(",", ":")),
                    ),
                )
            except sqlite3.IntegrityError as e:
                # Surface append-only violations via the typed exception
                # so callers can distinguish them from FK violations.
                raise translate_append_only_error(e) from e
        return evaluation

    def get_evaluation(self, evaluation_id: str) -> GovernanceEvaluation:
        row = self._conn.execute(
            "SELECT content_json FROM governance_evaluations "
            "WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
        if row is None:
            raise ArtifactNotFoundError(
                f"no governance_evaluation with evaluation_id={evaluation_id!r}"
            )
        return GovernanceEvaluation.from_dict(json.loads(row["content_json"]))

    def list_evaluations_for_artifact(
        self, artifact_id: str,
    ) -> List[GovernanceEvaluation]:
        """
        All evaluations performed against a given artifact, oldest first.
        Replay UI uses this to render the "same artifact, different
        policies/modes" comparison.
        """
        rows = self._conn.execute(
            "SELECT content_json FROM governance_evaluations "
            "WHERE artifact_id = ? ORDER BY created_at ASC",
            (artifact_id,),
        ).fetchall()
        return [
            GovernanceEvaluation.from_dict(json.loads(r["content_json"]))
            for r in rows
        ]


__all__ = ["ArtifactStore", "ArtifactNotFoundError", "AppendOnlyViolation"]
