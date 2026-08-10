"""FastAPI application entry point.

Run it with:  uvicorn app.main:app --reload
Swagger UI:   http://localhost:8000/docs
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.routes import health
from app.config import get_settings
from app.db.session import engine

settings = get_settings()


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
    ],
)

register_error_handlers(app)
app.include_router(health.router)
