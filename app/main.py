"""FastAPI application entry point.

Run it with:  uvicorn app.main:app --reload
Swagger UI:   http://localhost:8000/docs
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.routes import documents, health, rooms, students, turns
from app.config import get_settings
from app.db.session import engine

settings = get_settings()

# Without this the root logger sits at its WARNING default and every
# `logger.info` in the project is discarded — so the record of which document was
# indexed, which turn was repaired and which tool was cached existed in the code
# and nowhere else. `LOG_LEVEL` was a setting nothing read.
#
# `force=True` because uvicorn installs its own handlers first; without it this
# call is a no-op and the symptom is unchanged.
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    force=True,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once on startup, and again on shutdown after the yield."""
    yield
    await engine.dispose()  # close database connections cleanly


app = FastAPI(
    title="Porua AI",
    version="0.1.0",
    summary="Backend for an AI-powered study app.",
    description=(
        "Students create a **room** for one topic, chat with an AI tutor inside it, "
        "and upload their class files as source material.\n\n"
        "- A **room** is a topic. Created once, lives for weeks.\n"
        "- A **turn** is one question and answer inside a room. Every turn records "
        "the prompts used, every model attempt including failures, tool activity, "
        "and token usage — see `GET /turns/{turn_id}`."
    ),
    lifespan=lifespan,
    openapi_tags=[
        {"name": "health", "description": "Liveness and readiness checks."},
        {"name": "students", "description": "Demo students. Stands in for auth."},
        {"name": "rooms", "description": "Study rooms — one per topic."},
        {
            "name": "turns",
            "description": "One question and answer inside a room. Where the AI runs.",
        },
        {
            "name": "documents",
            "description": (
                "Study material uploaded to a room. The tutor searches it through "
                "the `search_room_materials` tool."
            ),
        },
    ],
)

register_error_handlers(app)
app.include_router(health.router)
app.include_router(students.router)
app.include_router(rooms.router)
app.include_router(turns.rooms_router)
app.include_router(turns.turns_router)
app.include_router(documents.rooms_router)
app.include_router(documents.documents_router)
