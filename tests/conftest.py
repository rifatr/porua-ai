"""Shared test fixtures.

Tests talk to the API through httpx's ASGITransport. That calls the FastAPI app
directly in-process — no uvicorn, no network, no port. Fast and deterministic.

Each test runs inside a transaction that is rolled back at the end, so tests never
see each other's rows and order does not matter.
"""

import os
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# Tests must never call the real Gemini API.
os.environ.setdefault("LLM_FIXTURE_MODE", "replay")

from app.config import get_settings  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, Student  # noqa: E402

settings = get_settings()


# The loop scope comes from asyncio_default_fixture_loop_scope in pyproject.toml,
# so it does not need repeating on each fixture.
@pytest.fixture(scope="session")
async def engine():
    # NullPool: every connection is opened and closed on demand, which avoids
    # pooled connections outliving the event loop between tests.
    eng = create_async_engine(settings.database_url, poolclass=NullPool)
    async with eng.begin() as conn:
        # Build the schema straight from the models. Migrations are verified
        # separately, in test_migrations.py.
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine) -> AsyncGenerator[AsyncSession, None]:
    """A session wrapped in a transaction that is always rolled back."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(bind=connection, expire_on_commit=False)
        async with factory() as session:
            yield session
        await transaction.rollback()


@pytest.fixture
async def client(db: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """An API client that shares the test's transaction.

    Overriding get_session means the endpoint writes into the same rolled-back
    transaction, so a test can assert on rows the endpoint created.
    """

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield db

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def student(db: AsyncSession) -> Student:
    s = Student(display_name="Test Student", education_level="Class 8")
    db.add(s)
    await db.flush()
    return s


@pytest.fixture
def auth(student: Student) -> dict[str, str]:
    """Headers identifying the caller. Stands in for a login token."""
    return {"X-Student-Id": str(student.id)}
