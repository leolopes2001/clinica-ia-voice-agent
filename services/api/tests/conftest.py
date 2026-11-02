import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_core.config import ConfigPostgres
from clinica_core.db import criar_engine

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


async def _ddl(*comandos: str) -> None:
    engine = criar_engine(ConfigPostgres()).execution_options(isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conexao:
            for comando in comandos:
                await conexao.execute(text(comando))
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def _postgres_no_ar() -> None:
    try:
        asyncio.run(_ddl("SELECT 1"))
    except OSError:
        pytest.skip("postgres indisponível: rode `docker compose up -d --wait`")


@contextmanager
def _banco_temporario() -> Iterator[str]:
    nome = f"teste_{uuid4().hex}"
    asyncio.run(_ddl(f"CREATE DATABASE {nome}"))
    try:
        with pytest.MonkeyPatch.context() as ambiente:
            ambiente.setenv("POSTGRES_DB", nome)
            yield nome
    finally:
        asyncio.run(_ddl(f"DROP DATABASE {nome} WITH (FORCE)"))


@pytest.fixture(scope="module")
def banco(_postgres_no_ar: None) -> Iterator[str]:
    with _banco_temporario() as nome:
        command.upgrade(Config(str(ALEMBIC_INI)), "head")
        yield nome


@pytest.fixture
def banco_vazio(_postgres_no_ar: None) -> Iterator[str]:
    with _banco_temporario() as nome:
        yield nome


@pytest.fixture
async def sessao(banco: str) -> AsyncIterator[AsyncSession]:
    engine = criar_engine(ConfigPostgres(db=banco))
    async with engine.connect() as conexao:
        transacao = await conexao.begin()
        async with AsyncSession(
            bind=conexao, join_transaction_mode="create_savepoint", expire_on_commit=False
        ) as sessao:
            yield sessao
        await transacao.rollback()
    await engine.dispose()
