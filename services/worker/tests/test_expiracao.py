import asyncio
import inspect
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aio_pika
import pytest
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from clinica_api.publicacao import LIMITE_PUBLICACAO_S, PublicadorRabbit
from clinica_core.config import ConfigPostgres, ConfigRabbit
from clinica_core.db import criar_engine, criar_fabrica_sessoes
from clinica_core.mensageria import (
    EXCHANGE_DLX,
    FILA_HOLDS_ATRASO,
    FILA_HOLDS_EXPIRAR,
    HoldSegurado,
    abrir_canal,
    conectar,
    declarar_topologia,
    publicar,
)
from clinica_core.modelos import (
    Agendamento,
    Clinica,
    Consultorio,
    Dentista,
    Paciente,
    Procedimento,
)
from clinica_worker.holds import expirar_hold, expirar_vencidos
from clinica_worker.principal import rodar
from clinica_worker.saude import saudavel

Fabrica = async_sessionmaker[AsyncSession]
CriarHold = Callable[..., Awaitable[UUID]]
INICIO = datetime(2031, 1, 6, 12, tzinfo=UTC)
CURTA = timedelta(milliseconds=100)


@pytest.fixture
async def engine(banco: str) -> AsyncIterator[AsyncEngine]:
    engine = criar_engine(ConfigPostgres(db=banco))
    yield engine
    await engine.dispose()


@pytest.fixture
def fabrica(engine: AsyncEngine) -> Fabrica:
    return criar_fabrica_sessoes(engine)


@pytest.fixture
async def criar_hold(fabrica: Fabrica) -> CriarHold:
    async with fabrica() as sessao:
        clinica = Clinica(nome="C", telefone="+5511", endereco="R", politica_cancelamento_horas=24)
        sessao.add(clinica)
        await sessao.flush()
        dentista = Dentista(clinica_id=clinica.id, nome="A")
        consultorio = Consultorio(clinica_id=clinica.id, nome="1")
        procedimento = Procedimento(clinica_id=clinica.id, nome="Avaliação", duracao_min=30)
        paciente = Paciente(
            clinica_id=clinica.id,
            nome="Maria",
            telefone_e164="+5511999990000",
            data_nascimento=date(1990, 1, 1),
        )
        sessao.add_all([dentista, consultorio, procedimento, paciente])
        await sessao.commit()
    deslocamento = {"horas": 0}

    async def criar(
        vencido: bool = True, status: str = "segurado", com_paciente: bool = False
    ) -> UUID:
        deslocamento["horas"] += 1
        inicio = INICIO + timedelta(hours=deslocamento["horas"])
        margem = timedelta(minutes=-1 if vencido else 3)
        async with fabrica() as sessao:
            hold = Agendamento(
                clinica_id=clinica.id,
                dentista_id=dentista.id,
                consultorio_id=consultorio.id,
                procedimento_id=procedimento.id,
                paciente_id=paciente.id if com_paciente else None,
                periodo=Range(inicio, inicio + timedelta(minutes=30)),
                status=status,
                expira_em=func.now() + margem,
                origem="painel",
            )
            sessao.add(hold)
            await sessao.commit()
            return hold.id

    return criar


async def _status(fabrica: Fabrica, hold_id: UUID) -> str:
    async with fabrica() as sessao:
        status = await sessao.scalar(select(Agendamento.status).where(Agendamento.id == hold_id))
    assert status is not None
    return status


async def _ate_que(condicao: Callable[[], bool | Awaitable[bool]]) -> None:
    async with asyncio.timeout(10):
        while True:
            resultado = condicao()
            if await resultado if inspect.isawaitable(resultado) else resultado:
                return
            await asyncio.sleep(0.02)


async def _espera_status(fabrica: Fabrica, hold_id: UUID, esperado: str) -> None:
    async def chegou() -> bool:
        return await _status(fabrica, hold_id) == esperado

    await _ate_que(chegou)


async def _receber(canal: AbstractChannel, nome: str) -> AbstractIncomingMessage:
    fila = await canal.get_queue(nome)
    async with asyncio.timeout(10):
        while True:
            mensagem = await fila.get(fail=False)
            if mensagem is not None:
                return mensagem
            await asyncio.sleep(0.02)


# ---- Regras de expiração


async def test_hold_vencido_expira(fabrica: Fabrica, criar_hold: CriarHold) -> None:
    hold_id = await criar_hold()
    async with fabrica() as sessao:
        assert await expirar_hold(sessao, hold_id) is True
        await sessao.commit()
    assert await _status(fabrica, hold_id) == "expirado"


async def test_expirar_de_novo_nao_tem_efeito(fabrica: Fabrica, criar_hold: CriarHold) -> None:
    hold_id = await criar_hold()
    async with fabrica() as sessao:
        assert await expirar_hold(sessao, hold_id) is True
        assert await expirar_hold(sessao, hold_id) is False
        await sessao.commit()


async def test_hold_valido_nao_expira(fabrica: Fabrica, criar_hold: CriarHold) -> None:
    hold_id = await criar_hold(vencido=False)
    async with fabrica() as sessao:
        assert await expirar_hold(sessao, hold_id) is False
        await sessao.commit()
    assert await _status(fabrica, hold_id) == "segurado"


async def test_consulta_confirmada_nao_expira(fabrica: Fabrica, criar_hold: CriarHold) -> None:
    hold_id = await criar_hold(status="confirmado", com_paciente=True)
    async with fabrica() as sessao:
        assert await expirar_hold(sessao, hold_id) is False
        await sessao.commit()
    assert await _status(fabrica, hold_id) == "confirmado"


async def test_hold_inexistente_nao_expira(fabrica: Fabrica) -> None:
    async with fabrica() as sessao:
        assert await expirar_hold(sessao, uuid4()) is False


async def test_varredura_expira_so_os_vencidos(fabrica: Fabrica, criar_hold: CriarHold) -> None:
    vencido = await criar_hold()
    valido = await criar_hold(vencido=False)
    confirmado = await criar_hold(status="confirmado", com_paciente=True)
    async with fabrica() as sessao:
        expirados = await expirar_vencidos(sessao)
        await sessao.commit()
    assert vencido in expirados
    assert valido not in expirados
    assert await _status(fabrica, vencido) == "expirado"
    assert await _status(fabrica, valido) == "segurado"
    assert await _status(fabrica, confirmado) == "confirmado"


async def test_varredura_pula_hold_travado_sem_esperar(
    fabrica: Fabrica, criar_hold: CriarHold
) -> None:
    hold_id = await criar_hold()
    async with fabrica() as trava:
        await trava.execute(
            select(Agendamento.id).where(Agendamento.id == hold_id).with_for_update()
        )
        async with fabrica() as sessao:
            async with asyncio.timeout(5):
                expirados = await expirar_vencidos(sessao)
            await sessao.commit()
        assert hold_id not in expirados
        await trava.rollback()
    assert await _status(fabrica, hold_id) == "segurado"


# ---- Worker de ponta a ponta


@pytest.fixture
async def worker(
    banco: str, vhost: str, tmp_path: Path, request: pytest.FixtureRequest
) -> AsyncIterator[Path]:
    arquivo = tmp_path / "vivo"
    parar = asyncio.Event()
    intervalo = getattr(request, "param", 3600.0)
    tarefa = asyncio.create_task(
        rodar(ConfigPostgres(db=banco), ConfigRabbit(), parar, intervalo, arquivo)
    )
    await _ate_que(arquivo.exists)
    yield arquivo
    parar.set()
    await asyncio.wait_for(tarefa, timeout=10)


@pytest.fixture
async def publicador(worker: Path) -> AsyncIterator[PublicadorRabbit]:
    publicador = PublicadorRabbit(ConfigRabbit())
    await publicador.abrir()
    await _ate_que(lambda: publicador._canal is not None)
    yield publicador
    await publicador.fechar()


async def test_hold_publicado_expira_pela_fila(
    fabrica: Fabrica, criar_hold: CriarHold, publicador: PublicadorRabbit
) -> None:
    hold_id = await criar_hold()
    await publicador.hold_criado(hold_id, CURTA)
    await _espera_status(fabrica, hold_id, "expirado")


async def test_hold_valido_continua_segurado_quando_a_mensagem_chega(
    fabrica: Fabrica,
    criar_hold: CriarHold,
    publicador: PublicadorRabbit,
    consultar_filas: Callable[[], Awaitable[dict[str, Any]]],
) -> None:
    hold_id = await criar_hold(vencido=False)
    await publicador.hold_criado(hold_id, CURTA)
    # A API de gerência atualiza as contagens com atraso: as duas filas precisam estar vazias.
    await asyncio.sleep(0.5)

    async def filas_vazias() -> bool:
        filas = await consultar_filas()
        return all(
            filas[f].get("messages", 1) == 0 for f in (FILA_HOLDS_ATRASO, FILA_HOLDS_EXPIRAR)
        )

    await _ate_que(filas_vazias)
    assert await _status(fabrica, hold_id) == "segurado"
    assert (await consultar_filas())[f"{FILA_HOLDS_EXPIRAR}.dlq"].get("messages", 0) == 0


async def test_mensagem_invalida_vai_para_a_dlq(worker: Path) -> None:
    conexao = await conectar(ConfigRabbit())
    try:
        canal = await abrir_canal(conexao)
        dlx = await canal.get_exchange(EXCHANGE_DLX)
        await dlx.publish(aio_pika.Message(b"nao e json"), routing_key=FILA_HOLDS_EXPIRAR)
        mensagem = await _receber(canal, f"{FILA_HOLDS_EXPIRAR}.dlq")
        await mensagem.ack()
        assert mensagem.body == b"nao e json"
    finally:
        await conexao.close()


@pytest.mark.parametrize("worker", [0.05], indirect=True)
async def test_varredura_periodica_expira_sem_mensagem(
    fabrica: Fabrica, criar_hold: CriarHold, worker: Path
) -> None:
    hold_id = await criar_hold()
    await _espera_status(fabrica, hold_id, "expirado")


@pytest.mark.parametrize("worker", [0.05], indirect=True)
async def test_varredura_atualiza_o_arquivo_de_saude(worker: Path) -> None:
    os.utime(worker, (0, 0))
    assert not saudavel(worker, 60)
    await _ate_que(lambda: saudavel(worker, 60))


async def test_worker_para_ao_receber_o_sinal(banco: str, vhost: str, tmp_path: Path) -> None:
    parar = asyncio.Event()
    arquivo = tmp_path / "vivo"
    tarefa = asyncio.create_task(
        rodar(ConfigPostgres(db=banco), ConfigRabbit(), parar, 3600, arquivo)
    )
    await _ate_que(arquivo.exists)
    parar.set()
    await asyncio.wait_for(tarefa, timeout=5)


# ---- Saúde


def test_saude_sem_arquivo(tmp_path: Path) -> None:
    assert not saudavel(tmp_path / "vivo", 60)


def test_saude_com_arquivo_recente_e_antigo(tmp_path: Path) -> None:
    arquivo = tmp_path / "vivo"
    arquivo.touch()
    assert saudavel(arquivo, 60)
    antigo = time.time() - 120
    os.utime(arquivo, (antigo, antigo))
    assert not saudavel(arquivo, 60)


# ---- Publicador: nunca derruba o agendamento


async def test_limite_de_publicacao_e_de_2_segundos() -> None:
    assert LIMITE_PUBLICACAO_S == 2.0


async def test_publicador_sem_conexao_so_avisa(caplog: pytest.LogCaptureFixture) -> None:
    publicador = PublicadorRabbit(ConfigRabbit())
    with caplog.at_level(logging.WARNING):
        await publicador.hold_criado(uuid4(), CURTA)
    assert "não publicado" in caplog.text


async def test_api_sobe_com_o_broker_fora_do_ar(caplog: pytest.LogCaptureFixture) -> None:
    publicador = PublicadorRabbit(ConfigRabbit(host="127.0.0.1", port=1))
    async with asyncio.timeout(1):
        await publicador.abrir()
    await asyncio.sleep(0.2)
    with caplog.at_level(logging.WARNING):
        await publicador.hold_criado(uuid4(), CURTA)
    assert "não publicado" in caplog.text
    await publicador.fechar()


class _CanalQualquer:
    pass


async def test_publicacao_travada_respeita_o_limite(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def travada(*_: Any, **__: Any) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr("clinica_api.publicacao.publicar", travada)
    publicador = PublicadorRabbit(ConfigRabbit(), limite_s=0.05)
    publicador._canal = _CanalQualquer()  # type: ignore[assignment]
    inicio = time.monotonic()
    with caplog.at_level(logging.WARNING):
        await publicador.hold_criado(uuid4(), CURTA)
    assert time.monotonic() - inicio < 1
    assert "não publicado" in caplog.text


async def test_erro_ao_publicar_so_avisa(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def falha(*_: Any, **__: Any) -> None:
        raise ConnectionError("broker caiu")

    monkeypatch.setattr("clinica_api.publicacao.publicar", falha)
    publicador = PublicadorRabbit(ConfigRabbit())
    publicador._canal = _CanalQualquer()  # type: ignore[assignment]
    with caplog.at_level(logging.WARNING):
        await publicador.hold_criado(uuid4(), CURTA)
    assert "broker caiu" in caplog.text


async def test_publicar_usa_a_expiracao_da_mensagem(vhost: str) -> None:
    conexao = await conectar(ConfigRabbit())
    try:
        canal = await abrir_canal(conexao)
        await declarar_topologia(canal)
        await publicar(canal, "holds.atraso", _Corpo(), expiracao=timedelta(seconds=30))
        mensagem = await _receber(canal, FILA_HOLDS_ATRASO)
        await mensagem.ack()
        assert mensagem.expiration == 30
    finally:
        await conexao.close()


class _Corpo(HoldSegurado):
    hold_id: UUID = UUID(int=1)
