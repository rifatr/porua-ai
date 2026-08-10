"""Database engine and session handling.

FastAPI note: `get_session` is a dependency. Endpoints declare
`db: AsyncSession = Depends(get_session)` and FastAPI runs this function first,
hands over the session, and closes it afterwards — even if the endpoint raised.
No endpoint ever opens or closes a connection by hand.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,  # drops dead connections instead of failing a request
)

SessionFactory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,  # objects stay usable after commit, for the response
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """One session per request, committed on success and rolled back on error."""
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
