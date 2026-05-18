"""
tcs.api.app
===========

FastAPI application factory for the TCS runtime control surface.

The factory returns a configured FastAPI instance. Tests inject an
in-memory ``CertificateStore``; production wires the factory to a
persistent store via a small entrypoint script (Phase 3).

App state carries three objects the routes read:

    app.state.store          — CertificateStore (persistence)
    app.state.interceptor    — RequestInterceptor (runtime pipeline)
    app.state.start_time     — datetime for /v2/health uptime

Dependency injection for routes uses a simple ``Depends`` pattern that
reads from ``request.app.state``. This keeps the routes testable
without any module-level singletons.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, JSONResponse

from tcs.persistence import CertificateStore
from tcs.sidecar import RequestInterceptor

from tcs.api.routes_govern import router as govern_router
from tcs.api.routes_certificates import router as certificates_router
from tcs.api.routes_metrics import router as metrics_router
from tcs.api.routes_dynamics import router as dynamics_router
from tcs.api.routes_pll import router as pll_router
from tcs.api.routes_recovery import router as recovery_router
from tcs.api.routes_simulation import router as simulation_router
from tcs.api.routes_packs import router as packs_router
from tcs.api.routes_standards import router as standards_router
from tcs.api.routes_admin import router as admin_router
from tcs.api.routes_auth import router as auth_router
from tcs.api.routes_query import router as query_router
from tcs.api.routes_connections import router as connections_router
from tcs.api.routes_archive import router as archive_router


#: Version surfaced via /v2/health. Bumped when the API contract or
#: canonical policy profiles change.
API_VERSION = "0.2.0"

#: Policy set version surfaced via /v2/health. Matches the composite
#: identifier emitted by governed_context.CT_MODIFIER_ID.
POLICY_VERSION = "ct-modifiers-v1-2026-04"


def create_app(
    *,
    store: Optional[CertificateStore] = None,
    interceptor: Optional[RequestInterceptor] = None,
) -> FastAPI:
    """
    Build a FastAPI instance with all routes wired.

    Parameters
    ----------
    store
        CertificateStore to attach to ``app.state.store``. If None, a
        fresh store is created at the default on-disk location.
    interceptor
        RequestInterceptor to attach. If None, a fresh one is wired
        to the supplied (or default) store.

    Returns
    -------
    FastAPI
        Application with three routers mounted:
            * /v2/govern         (routes_govern)
            * /v2/certificates   (routes_certificates)
            * /v2/metrics, /v2/health (routes_metrics)
    """
    owns_store = False
    if store is None:
        backend = os.environ.get("TCS_DB_BACKEND", "sqlite").lower()
        if backend == "postgres":
            from tcs.persistence.pg_store import PostgresCertificateStore
            store = PostgresCertificateStore()
            store.run_migrations()
        else:
            store = CertificateStore()
        owns_store = True

    if interceptor is None:
        interceptor = RequestInterceptor(store)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """
        Modern FastAPI lifespan handler. Replaces the deprecated
        on_event('shutdown') hook. Only closes the store we created
        ourselves — caller-injected stores are the caller's to manage.
        """
        try:
            yield
        finally:
            if getattr(app.state, "owns_store", False):
                try:
                    app.state.store.close()
                except Exception:  # noqa: BLE001
                    pass

    app = FastAPI(
        title="Trust Computation System — Runtime API",
        version=API_VERSION,
        description=(
            "Runtime governance surface for the Trust Computation System "
            "v0.1 reference implementation. Not a production service — "
            "designed to prove the TCS math is controllable at runtime."
        ),
        lifespan=lifespan,
    )

    # Stash state the routes read via request.app.state
    app.state.store = store
    app.state.interceptor = interceptor
    app.state.start_time = datetime.now(timezone.utc)
    app.state.api_version = API_VERSION
    app.state.policy_version = POLICY_VERSION
    app.state.owns_store = owns_store
    app.state.rbac_enabled = False  # off by default for backward compat

    # Allow the dashboard to call the API from the browser
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # RBAC middleware — enforces role-based access when enabled
    class RBACMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if not getattr(request.app.state, "rbac_enabled", False):
                return await call_next(request)

            # Skip RBAC for OpenAPI docs, health, login, and static assets
            path = request.url.path
            if path in ("/docs", "/openapi.json", "/redoc"):
                return await call_next(request)
            if path.startswith("/v2/auth/"):
                return await call_next(request)
            if path.startswith("/assets/") or not path.startswith("/v2/"):
                return await call_next(request)

            from tcs.identity.session import get_session
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Missing or invalid Authorization header"},
                )
            token = auth[7:]
            session = get_session(token)
            if session is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or expired session token"},
                )
            if not session.can_access(request.method, path):
                return JSONResponse(
                    status_code=403,
                    content={"detail": f"Role(s) {session.role_names} not authorized for {request.method} {path}"},
                )
            return await call_next(request)

    app.add_middleware(RBACMiddleware)

    # Mount routers
    app.include_router(govern_router, prefix="/v2", tags=["govern"])
    app.include_router(certificates_router, prefix="/v2", tags=["certificates"])
    app.include_router(metrics_router, prefix="/v2", tags=["metrics"])
    app.include_router(dynamics_router, prefix="/v2", tags=["dynamics"])
    app.include_router(pll_router, prefix="/v2", tags=["pll"])
    app.include_router(recovery_router, prefix="/v2", tags=["recovery"])
    app.include_router(simulation_router, prefix="/v2", tags=["simulation"])
    app.include_router(packs_router, prefix="/v2", tags=["packs"])
    app.include_router(standards_router, prefix="/v2", tags=["standards"])
    app.include_router(admin_router, prefix="/v2", tags=["admin"])
    app.include_router(auth_router, prefix="/v2", tags=["auth"])
    app.include_router(query_router, prefix="/v2", tags=["query"])
    app.include_router(connections_router, prefix="/v2", tags=["connections"])
    app.include_router(archive_router, prefix="/v2", tags=["archives"])

    # --- Static file serving for frontend build output ------------------- #
    _frontend_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    if _frontend_dist.is_dir():
        app.mount("/assets", StaticFiles(directory=str(_frontend_dist / "assets")), name="assets")

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str):
            """Serve the SPA index.html for all non-API routes."""
            file_path = _frontend_dist / full_path
            if full_path and file_path.is_file():
                return FileResponse(str(file_path))
            return FileResponse(str(_frontend_dist / "index.html"))

    return app


#: Module-level app instance for `uvicorn tcs.api.app:app` usage.
#: Uses the default on-disk store.
app = create_app()
