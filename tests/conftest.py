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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# Tests must never call the real Gemini API.
os.environ.setdefault("LLM_FIXTURE_MODE", "replay")
# The retry loop is real, and so is its backoff. Left at the production 0.5s, a
# handful of retry tests would add several seconds of pure sleeping to every run.
# Set to zero the loop still runs exactly as it does in production — it just does
# not wait, so the tests measure the logic rather than the clock.
os.environ.setdefault("RETRY_BASE_DELAY_SECONDS", "0")

from app.api.deps import get_llm  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, Student  # noqa: E402
from tests.fakes import FakeLLM  # noqa: E402

settings = get_settings()


def _test_database_url() -> str:
    """A separate database for tests, alongside the development one.

    Tests must not share a database with the running app. Rolling each test back
    protects against tests seeing *each other*, but it does nothing about rows a
    developer created by hand with curl — those are committed, and a query like
    `select(Turn)` would happily return them. That produces failures which look
    like application bugs and are not.
    """
    base = settings.database_url
    name = base.rsplit("/", 1)[-1]
    return base.rsplit("/", 1)[0] + f"/{name}_test"


async def _create_test_database_if_missing() -> None:
    """CREATE DATABASE cannot run inside a transaction, hence AUTOCOMMIT."""
    admin = create_async_engine(settings.database_url, poolclass=NullPool)
    target = _test_database_url().rsplit("/", 1)[-1]
    try:
        async with admin.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target}
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{target}"'))
    finally:
        await admin.dispose()


# The loop scope comes from asyncio_default_fixture_loop_scope in pyproject.toml,
# so it does not need repeating on each fixture.
@pytest.fixture(scope="session")
async def engine():
    await _create_test_database_if_missing()
    # NullPool: every connection is opened and closed on demand, which avoids
    # pooled connections outliving the event loop between tests.
    eng = create_async_engine(_test_database_url(), poolclass=NullPool)
    async with eng.begin() as conn:
        # Build the schema straight from the models. Migrations are verified
        # separately by their own up/down round trip.
        await conn.run_sync(Base.metadata.drop_all)
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
def fake_llm() -> FakeLLM:
    """The stand-in model used by every test that runs a turn."""
    return FakeLLM()


@pytest.fixture
async def client(db: AsyncSession, fake_llm: FakeLLM) -> AsyncGenerator[AsyncClient, None]:
    """An API client that shares the test's transaction and a fake model.

    Overriding get_session means the endpoint writes into the same rolled-back
    transaction, so a test can assert on rows the endpoint created.

    Overriding get_llm is what keeps the suite offline. Nothing here can reach
    Google, so tests are free, fast, and give the same answer every run.
    """

    async def _session() -> AsyncGenerator[AsyncSession, None]:
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_llm] = lambda: fake_llm
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
