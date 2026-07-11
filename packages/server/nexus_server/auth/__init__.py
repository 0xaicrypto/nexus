"""Auth domain — username/password login + JWT verification.

Public surface (what other server modules / tests import):

    from nexus_server.auth import (
        router,                # FastAPI router for /api/v1/auth/*
        get_current_user,      # dependency for authenticated routes
        create_jwt, verify_jwt,
    )
"""

from .routes import *  # noqa: F401, F403  — re-export for back-compat
from .routes import (
    router,
    get_current_user,
    create_jwt_token,
    verify_jwt_token,
)
