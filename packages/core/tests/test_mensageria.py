import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage, AbstractRobustConnection
from aio_pika.exceptions import DeliveryError
from pydantic import BaseModel

from clinica_core.config import ConfigRabbit
from clinica_core.mensageria import (
    FILA_CHAMADA_ENCERRADA,
    FILA_CONHECIMENTO_INDEXAR,
    FILA_HOLDS_ATRASO,
    FILA_HOLDS_EXPIRAR,
    FILA_MANUTENCAO_DIARIA,
    abrir_canal,
    conectar,
    declarar_fila_painel,
    declarar_topologia,
    publicar,
)

FILAS_DURAVEIS = {
    FILA_HOLDS_ATRASO,
    FILA_HOLDS_EXPIRAR,
    FILA_CONHECIMENTO_INDEXAR,
    FILA_CHAMADA_ENCERRADA,
    FILA_MANUTENCAO_DIARIA,
    f"{FILA_HOLDS_EXPIRAR}.dlq",
    f"{FILA_CONHECIMENTO_INDEXAR}.dlq",
    f"{FILA_CHAMADA_ENCERRADA}.dlq",
    f"{FILA_MANUTENCAO_DIARIA}.dlq",
}


EXPIRACAO_CURTA = timedelta(milliseconds=50)
ConsultarFilas = Callable[[], Awaitable[dict[str, Any]]]


class Evento(BaseModel):
    id: int = 1


@pytest.fixture
async def conexao(vhost: str) -> AsyncIterator[AbstractRobustConnection]:
    conexao = await conectar(ConfigRabbit())
    yield conexao
    await conexao.close()


@pytest.fixture
async def canal(conexao: AbstractRobustConnection) -> AbstractChannel:
    return await abrir_canal(conexao)


async def _receber(canal: AbstractChannel, nome: str) -> AbstractIncomingMessage:
    fila = await canal.get_queue(nome)
    async with asyncio.timeout(5):
        while True:
            mensagem = await fila.get(fail=False)
            if mensagem is not None:
                return mensagem
            await asyncio.sleep(0.02)


async def test_topologia_tem_as_filas_da_spec(
    canal: AbstractChannel, consultar_filas: ConsultarFilas
) -> None:
    await declarar_topologia(canal)
    filas = await consultar_filas()
    assert set(filas) == FILAS_DURAVEIS
    assert all(fila["durable"] for fila in filas.values())
    # A duração vem de cada mensagem e muda sem recriar a fila.
    assert "x-message-ttl" not in filas[FILA_HOLDS_ATRASO]["arguments"]


async def test_declarar_duas_vezes_nao_da_erro(canal: AbstractChannel) -> None:
    await declarar_topologia(canal)
    await declarar_topologia(canal)


async def test_hold_vencido_chega_em_holds_expirar(canal: AbstractChannel) -> None:
    await declarar_topologia(canal)
    await publicar(canal, FILA_HOLDS_ATRASO, Evento(id=7), expiracao=EXPIRACAO_CURTA)
    mensagem = await _receber(canal, FILA_HOLDS_EXPIRAR)
    await mensagem.ack()
    assert Evento.model_validate_json(mensagem.body).id == 7


@pytest.mark.parametrize(
    ("routing_key", "fila"),
    [
        (FILA_HOLDS_ATRASO, FILA_HOLDS_EXPIRAR),
        (FILA_CONHECIMENTO_INDEXAR, FILA_CONHECIMENTO_INDEXAR),
        (FILA_CHAMADA_ENCERRADA, FILA_CHAMADA_ENCERRADA),
        (FILA_MANUTENCAO_DIARIA, FILA_MANUTENCAO_DIARIA),
    ],
)
async def test_mensagem_rejeitada_cai_na_dlq(
    canal: AbstractChannel, routing_key: str, fila: str
) -> None:
    await declarar_topologia(canal)
    expiracao = EXPIRACAO_CURTA if routing_key == FILA_HOLDS_ATRASO else None
    await publicar(canal, routing_key, Evento(id=9), expiracao=expiracao)
    await (await _receber(canal, fila)).reject(requeue=False)
    mensagem = await _receber(canal, f"{fila}.dlq")
    await mensagem.ack()
    assert Evento.model_validate_json(mensagem.body).id == 9


async def test_holds_com_duracoes_diferentes_expiram_sem_recriar_a_fila(
    canal: AbstractChannel,
) -> None:
    await declarar_topologia(canal)
    await publicar(canal, FILA_HOLDS_ATRASO, Evento(id=1), expiracao=EXPIRACAO_CURTA)
    await publicar(canal, FILA_HOLDS_ATRASO, Evento(id=2), expiracao=2 * EXPIRACAO_CURTA)
    for esperado in (1, 2):
        mensagem = await _receber(canal, FILA_HOLDS_EXPIRAR)
        await mensagem.ack()
        assert Evento.model_validate_json(mensagem.body).id == esperado


async def test_publicar_sem_destino_da_erro(canal: AbstractChannel) -> None:
    await declarar_topologia(canal)
    with pytest.raises(DeliveryError):
        await publicar(canal, "inexistente.evento", Evento())


async def test_agendamento_sem_painel_e_descartado_sem_erro(
    canal: AbstractChannel, vhost: str
) -> None:
    await declarar_topologia(canal)
    await publicar(canal, "agendamento.confirmado", Evento())
    fila = await declarar_fila_painel(canal)
    assert await fila.get(fail=False) is None


async def test_cada_instancia_do_painel_recebe_o_evento(canal: AbstractChannel, vhost: str) -> None:
    await declarar_topologia(canal)
    outra_conexao = await conectar(ConfigRabbit())
    outro_canal = await abrir_canal(outra_conexao)
    try:
        fila_a = await declarar_fila_painel(canal)
        fila_b = await declarar_fila_painel(outro_canal)
        await publicar(canal, "agendamento.cancelado", Evento(id=3))

        for canal_painel, fila in ((canal, fila_a), (outro_canal, fila_b)):
            mensagem = await _receber(canal_painel, fila.name)
            await mensagem.ack()
            assert Evento.model_validate_json(mensagem.body).id == 3
    finally:
        await outra_conexao.close()


async def test_fila_do_painel_e_exclusiva_e_some_com_a_conexao(
    canal: AbstractChannel, consultar_filas: ConsultarFilas
) -> None:
    await declarar_topologia(canal)
    conexao_painel = await conectar(ConfigRabbit())
    fila = await declarar_fila_painel(await abrir_canal(conexao_painel))

    info = (await consultar_filas())[fila.name]
    assert info["exclusive"] is True
    assert info["auto_delete"] is True
    assert info["durable"] is False
    assert info["arguments"] == {}

    await conexao_painel.close()
    async with asyncio.timeout(5):
        while True:
            if fila.name not in await consultar_filas():
                return
            await asyncio.sleep(0.05)
