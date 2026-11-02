import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from aio_pika.abc import AbstractChannel
from pydantic import BaseModel

from clinica_core.mensageria import LIMITE_PUBLICACAO_S, publicar
from clinica_core.modelos import Agendamento

logger = logging.getLogger(__name__)

TipoEvento = Literal["criado", "confirmado", "remarcado", "cancelado", "expirado"]


class EventoAgendamento(BaseModel):
    """Só o que o painel precisa para saber o que atualizar: nada sobre o paciente (LGPD)."""

    tipo: TipoEvento
    agendamento_id: UUID
    clinica_id: UUID
    dentista_id: UUID
    consultorio_id: UUID
    status: str
    inicio: datetime
    fim_periodo: datetime
    # Só em "remarcado": a consulta cancelada, que libera outro horário na agenda.
    anterior: Self | None = None

    @property
    def routing_key(self) -> str:
        return f"agendamento.{self.tipo}"


def criar_evento(
    agendamento: Agendamento, tipo: TipoEvento, anterior: EventoAgendamento | None = None
) -> EventoAgendamento:
    # Os limites sempre existem: a API só grava [inicio, fim).
    assert agendamento.periodo.lower is not None
    assert agendamento.periodo.upper is not None
    return EventoAgendamento(
        tipo=tipo,
        agendamento_id=agendamento.id,
        clinica_id=agendamento.clinica_id,
        dentista_id=agendamento.dentista_id,
        consultorio_id=agendamento.consultorio_id,
        status=agendamento.status,
        inicio=agendamento.periodo.lower,
        fim_periodo=agendamento.periodo.upper,
        anterior=anterior,
    )


async def publicar_eventos(
    canal: AbstractChannel,
    eventos: Sequence[EventoAgendamento],
    limite_s: float = LIMITE_PUBLICACAO_S,
) -> None:
    """Nunca derruba quem chama: a mudança já foi gravada, e a agenda vem sempre do banco."""
    try:
        async with asyncio.timeout(limite_s):
            for evento in eventos:
                await publicar(canal, evento.routing_key, evento)
    except Exception as erro:
        ids = [str(evento.agendamento_id) for evento in eventos]
        logger.warning("Eventos de %s não publicados: %r", ids, erro)
