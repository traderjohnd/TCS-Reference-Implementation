"""
tcs.api
=======

FastAPI surface for the TCS runtime governance layer.

The app factory lives in :mod:`tcs.api.app`. Routes are split by
resource:

    routes_govern        POST /v1/govern
    routes_certificates  GET  /v1/certificates/{id}
    routes_metrics       GET  /v1/metrics/live
                         GET  /v1/health

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

__all__ = ["create_app"]
