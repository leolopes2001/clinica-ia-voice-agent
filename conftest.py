import asyncio
import base64
import json
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_core.config import ConfigPostgres, ConfigRabbit
from clinica_core.db import criar_engine

ALEMBIC_INI = Path(__file__).resolve().parent / "services" / "api" / "alembic.ini"


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


def _gerencia(metodo: str, caminho: str, corpo: dict[str, str] | None = None) -> Any:
    config = ConfigRabbit()
    credenciais = f"{config.user}:{config.password.get_secret_value()}".encode()
    pedido = urllib.request.Request(
        f"http://{config.host}:15672/api/{caminho}",
        method=metodo,
        data=json.dumps(corpo).encode() if corpo is not None else None,
        headers={
            "Authorization": f"Basic {base64.b64encode(credenciais).decode()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(pedido, timeout=5) as resposta:
        conteudo = resposta.read()
    return json.loads(conteudo) if conteudo else None


@pytest.fixture
def vhost(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    nome = f"teste_{uuid4().hex}"
    usuario = ConfigRabbit().user
    try:
        _gerencia("PUT", f"vhosts/{nome}", {})
    except (urllib.error.URLError, OSError):
        pytest.skip("rabbitmq indisponível: rode `docker compose up -d --wait`")
    _gerencia(
        "PUT",
        f"permissions/{nome}/{usuario}",
        {"configure": ".*", "write": ".*", "read": ".*"},
    )
    monkeypatch.setenv("RABBITMQ_VHOST", nome)
    yield nome
    _gerencia("DELETE", f"vhosts/{nome}")


@pytest.fixture
def consultar_filas(vhost: str) -> Callable[[], Awaitable[dict[str, Any]]]:
    async def consultar() -> dict[str, Any]:
        filas = await asyncio.to_thread(_gerencia, "GET", f"queues/{vhost}")
        return {fila["name"]: fila for fila in filas}

    return consultar
