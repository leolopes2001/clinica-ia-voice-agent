from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from google.adk.events import Event
from google.adk.sessions import DatabaseSessionService, InMemorySessionService
from google.genai import types
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

from clinica_agent.principal import APP, USUARIO_ADK, apagar_sessoes_abandonadas, encerrar_sessao
from clinica_core.config import ConfigPostgres, ConfigRabbit, ConfigSessoesAdk


def _url(banco: str) -> str:
    postgres = ConfigPostgres()
    return ConfigSessoesAdk(host=postgres.host, port=postgres.port, db=banco).url


@pytest.fixture
async def servico(banco: str) -> AsyncIterator[DatabaseSessionService]:
    servico = DatabaseSessionService(_url(banco))
    await servico.prepare_tables()
    yield servico
    await servico.close()


async def test_sessao_e_gravada_no_schema_proprio(
    servico: DatabaseSessionService, banco: str
) -> None:
    await servico.create_session(app_name=APP, user_id=USUARIO_ADK, session_id="s1")
    engine = create_async_engine(_url(banco))
    async with engine.connect() as conexao:
        schema = await conexao.scalar(
            text("SELECT schemaname FROM pg_tables WHERE tablename = 'sessions'")
        )
    await engine.dispose()
    assert schema == "adk_sessoes"


@pytest.mark.parametrize("tabela", ["public.chamadas", "public.pacientes", "public.agendamentos"])
async def test_usuario_das_sessoes_nao_le_a_clinica(banco: str, tabela: str) -> None:
    engine = create_async_engine(_url(banco))
    try:
        async with engine.connect() as conexao:
            with pytest.raises(ProgrammingError, match="permission denied"):
                await conexao.execute(text(f"SELECT * FROM {tabela}"))
    finally:
        await engine.dispose()


async def test_sessao_e_apagada_ao_encerrar_mesmo_sem_broker(
    servico: DatabaseSessionService,
) -> None:
    sessao = await servico.create_session(app_name=APP, user_id=USUARIO_ADK, session_id="s2")
    await servico.append_event(
        sessao,
        Event(author="user", input_transcription=types.Transcription(text="Meu dente dói")),
    )
    sem_broker = ConfigRabbit(host="127.0.0.1", port=1)
    chamada_id = "00000000-0000-0000-0000-000000000001"
    await encerrar_sessao(servico, "s2", chamada_id, sem_broker)
    apagada = await servico.get_session(app_name=APP, user_id=USUARIO_ADK, session_id="s2")
    assert apagada is None


async def test_apaga_so_as_sessoes_abandonadas() -> None:
    servico = InMemorySessionService()
    await servico.create_session(app_name=APP, user_id=USUARIO_ADK, session_id="atual")
    agora = datetime.now(UTC)
    assert await apagar_sessoes_abandonadas(servico, agora) == 0
    assert await apagar_sessoes_abandonadas(servico, agora + timedelta(days=2)) == 1
    restante = await servico.get_session(app_name=APP, user_id=USUARIO_ADK, session_id="atual")
    assert restante is None
