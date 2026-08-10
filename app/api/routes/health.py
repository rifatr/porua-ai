"""Health checks.

/healthz  - the process is up. Used by humans and by `docker compose`.
/readyz   - the process is up AND the database answers. Used before sending traffic.

They are separate on purpose: a failing database should not make the container
look dead and get restarted in a loop.
"""

import logging

from fastapi import APIRouter, status
from sqlalchemy import text

from app.api.deps import DbSession
from app.api.errors import AppError

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


class DatabaseUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "DATABASE_UNAVAILABLE"
    title = "Database unavailable"


@router.get("/healthz", summary="Liveness check")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness check (includes the database)")
async def readyz(db: DbSession) -> dict[str, str]:
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        # Log the real cause; the client only gets the generic message, because
        # connection strings and driver internals should not leave the server.
        logger.exception("readiness check failed")
        raise DatabaseUnavailableError("Could not reach the database.") from exc
    return {"status": "ready", "database": "ok"}
