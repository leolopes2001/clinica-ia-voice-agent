from uuid import uuid4

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractQueue,
    AbstractRobustConnection,
)
from pydantic import BaseModel

from clinica_core.config import ConfigRabbit

EXCHANGE_EVENTOS = "clinica.eventos"
EXCHANGE_DLX = "clinica.dlx"

FILA_HOLDS_ATRASO = "holds.atraso"
FILA_HOLDS_EXPIRAR = "holds.expirar"
FILA_CONHECIMENTO_INDEXAR = "conhecimento.indexar"
FILA_CHAMADA_ENCERRADA = "chamada.encerrada"
FILA_MANUTENCAO_DIARIA = "manutencao.diaria"

PADRAO_AGENDAMENTO = "agendamento.*"
TTL_HOLD_MS = 180_000


async def conectar(config: ConfigRabbit) -> AbstractRobustConnection:
    return await aio_pika.connect_robust(config.url)


async def abrir_canal(conexao: AbstractRobustConnection) -> AbstractChannel:
    # Sem on_return_raises, uma mensagem sem fila de destino se perde em silêncio.
    return await conexao.channel(publisher_confirms=True, on_return_raises=True)


async def _declarar_dlq(canal: AbstractChannel, dlx: AbstractExchange, fila: str) -> None:
    dlq = await canal.declare_queue(f"{fila}.dlq", durable=True)
    await dlq.bind(dlx, routing_key=dlq.name)


async def declarar_topologia(canal: AbstractChannel, ttl_hold_ms: int = TTL_HOLD_MS) -> None:
    eventos = await canal.declare_exchange(
        EXCHANGE_EVENTOS, aio_pika.ExchangeType.TOPIC, durable=True
    )
    dlx = await canal.declare_exchange(EXCHANGE_DLX, aio_pika.ExchangeType.DIRECT, durable=True)

    # Ao vencer o TTL, a mensagem de holds.atraso cai em holds.expirar pelo DLX.
    atraso = await canal.declare_queue(
        FILA_HOLDS_ATRASO,
        durable=True,
        arguments={
            "x-message-ttl": ttl_hold_ms,
            "x-dead-letter-exchange": EXCHANGE_DLX,
            "x-dead-letter-routing-key": FILA_HOLDS_EXPIRAR,
        },
    )
    await atraso.bind(eventos, routing_key=FILA_HOLDS_ATRASO)

    await _declarar_dlq(canal, dlx, FILA_HOLDS_EXPIRAR)
    expirar = await canal.declare_queue(
        FILA_HOLDS_EXPIRAR,
        durable=True,
        arguments={
            "x-dead-letter-exchange": EXCHANGE_DLX,
            "x-dead-letter-routing-key": f"{FILA_HOLDS_EXPIRAR}.dlq",
        },
    )
    await expirar.bind(dlx, routing_key=FILA_HOLDS_EXPIRAR)

    for nome in (FILA_CONHECIMENTO_INDEXAR, FILA_CHAMADA_ENCERRADA, FILA_MANUTENCAO_DIARIA):
        await _declarar_dlq(canal, dlx, nome)
        fila = await canal.declare_queue(
            nome,
            durable=True,
            arguments={
                "x-dead-letter-exchange": EXCHANGE_DLX,
                "x-dead-letter-routing-key": f"{nome}.dlq",
            },
        )
        await fila.bind(eventos, routing_key=nome)


async def declarar_fila_painel(canal: AbstractChannel) -> AbstractQueue:
    """Fila de um processo do painel: some com a conexão e não acumula eventos velhos."""
    eventos = await canal.get_exchange(EXCHANGE_EVENTOS)
    fila = await canal.declare_queue(
        f"painel.agendamentos.{uuid4()}", exclusive=True, auto_delete=True
    )
    await fila.bind(eventos, routing_key=PADRAO_AGENDAMENTO)
    return fila


async def publicar(canal: AbstractChannel, routing_key: str, corpo: BaseModel) -> None:
    eventos = await canal.get_exchange(EXCHANGE_EVENTOS, ensure=False)
    await eventos.publish(
        aio_pika.Message(
            corpo.model_dump_json().encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=routing_key,
        # Sem painel no ar, agendamento.* não tem fila, e isso não é erro.
        mandatory=not routing_key.startswith("agendamento."),
    )
