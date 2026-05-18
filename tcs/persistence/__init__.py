"""
tcs.persistence
===============

Persistent storage for Trust Certificates, lifecycle events, trust metrics,
and request audit records.

Public API:

    from tcs.persistence import CertificateStore, init_db

    store = CertificateStore("data/tcs.db")
    sequenced_tc = store.issue(tc)          # writes append-only + sets chain linkage
    loaded_tc = store.get(certificate_id)   # re-hydrate from DB
    ok = store.verify_chain(chain_id)       # C-R.18 hash-chain integrity check

Phase 2 uses SQLite. Phase 4 adds PostgreSQL as an alternative backend
selectable via ``TCS_DB_BACKEND=postgres``.
"""

from tcs.persistence.db import (
    init_db,
    open_connection,
    DEFAULT_DB_PATH,
    AppendOnlyViolation,
)
from tcs.persistence.certificate_store import (
    CertificateStore,
    ChainSequenceError,
    CertificateNotFoundError,
)

__all__ = [
    "init_db",
    "open_connection",
    "DEFAULT_DB_PATH",
    "AppendOnlyViolation",
    "CertificateStore",
    "ChainSequenceError",
    "CertificateNotFoundError",
]

# PostgresCertificateStore is available only when psycopg is installed.
# Import lazily to avoid breaking the rest of TCS on systems without it.
try:
    from tcs.persistence.pg_store import PostgresCertificateStore
    __all__.append("PostgresCertificateStore")
except ImportError:
    pass
