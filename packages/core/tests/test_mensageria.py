import asyncio
import base64
import json
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

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
    TTL_HOLD_MS,
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


class Evento(BaseModel):
    id: int = 1


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


async def _filas(vhost: str) -> dict[str, Any]:
    filas = await asyncio.to_thread(_gerencia, "GET", f"queues/{vhost}")
    return {fila["name"]: fila for fila in filas}


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


async def test_topologia_tem_as_filas_da_spec(canal: AbstractChannel, vhost: str) -> None:
    await declarar_topologia(canal)
    filas = await _filas(vhost)
    assert set(filas) == FILAS_DURAVEIS
    assert all(fila["durable"] for fila in filas.values())
    assert filas[FILA_HOLDS_ATRASO]["arguments"]["x-message-ttl"] == TTL_HOLD_MS == 180_000


async def test_declarar_duas_vezes_nao_da_erro(canal: AbstractChannel) -> None:
    await declarar_topologia(canal)
    await declarar_topologia(canal)


async def test_hold_vencido_chega_em_holds_expirar(canal: AbstractChannel) -> None:
    await declarar_topologia(canal, ttl_hold_ms=50)
    await publicar(canal, FILA_HOLDS_ATRASO, Evento(id=7))
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
    await declarar_topologia(canal, ttl_hold_ms=50)
    await publicar(canal, routing_key, Evento(id=9))
    await (await _receber(canal, fila)).reject(requeue=False)
    mensagem = await _receber(canal, f"{fila}.dlq")
    await mensagem.ack()
    assert Evento.model_validate_json(mensagem.body).id == 9


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
    canal: AbstractChannel, vhost: str
) -> None:
    await declarar_topologia(canal)
    conexao_painel = await conectar(ConfigRabbit())
    fila = await declarar_fila_painel(await abrir_canal(conexao_painel))

    info = (await _filas(vhost))[fila.name]
    assert info["exclusive"] is True
    assert info["auto_delete"] is True
    assert info["durable"] is False
    assert info["arguments"] == {}

    await conexao_painel.close()
    async with asyncio.timeout(5):
        while True:
            if fila.name not in await _filas(vhost):
                return
            await asyncio.sleep(0.05)
