from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from clinica_core.config import ConfigPostgres


def criar_engine(config: ConfigPostgres) -> AsyncEngine:
    return create_async_engine(config.url, pool_pre_ping=True)


def criar_fabrica_sessoes(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
