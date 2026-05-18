"""
tcs.api
=======

FastAPI surface for the TCS runtime governance layer.

The app factory lives in :mod:`tcs.api.app`. Routes are split by
resource:

    routes_govern        POST /v2/govern
    routes_certificates  GET  /v2/certificates/{id}
    routes_metrics       GET  /v2/metrics/live
                         GET  /v2/health

Typical usage:

    from tcs.api import create_app
    app = create_app()          # default store on data/tcs.db
    # uvicorn tcs.api.app:app   # or wire via the factory

Test usage:

    from fastapi.testclient import TestClient
    from tcs.api import create_app
    from tcs.persistence import CertificateStore

    store = CertificateStore(":memory:")
    app = create_app(store=store)
    client = TestClient(app)
"""

from tcs.api.app import create_app

# Module-level app instance for uvicorn CLI:
#   uvicorn tcs.api:app --port 8000
app = create_app()

__all__ = ["create_app", "app"]
