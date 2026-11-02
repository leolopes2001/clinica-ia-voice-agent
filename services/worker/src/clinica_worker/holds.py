import logging
from uuid import UUID

from aio_pika.abc import AbstractChannel, AbstractIncomingMessage
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clinica_core.eventos import EventoAgendamento, criar_evento, publicar_eventos
from clinica_core.mensageria import HoldSegurado
from clinica_core.modelos import Agendamento

logger = logging.getLogger(__name__)


async def expirar_hold(sessao: AsyncSession, hold_id: UUID) -> EventoAgendamento | None:
    expirado = await sessao.scalar(
        update(Agendamento)
        .where(
            Agendamento.id == hold_id,
            Agendamento.status == "segurado",
            Agendamento.expira_em <= func.now(),
        )
        .values(status="expirado")
        .returning(Agendamento)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    return None if expirado is None else criar_evento(expirado, "expirado")


async def expirar_vencidos(sessao: AsyncSession) -> list[EventoAgendamento]:
    # SKIP LOCKED: a varredura nunca espera uma transação de segurar, então não forma deadlock.
    vencidos = (
        select(Agendamento.id)
        .where(Agendamento.status == "segurado", Agendamento.expira_em <= func.now())
        .with_for_update(skip_locked=True)
    )
    expirados = await sessao.scalars(
        update(Agendamento)
        .where(Agendamento.id.in_(vencidos))
        .values(status="expirado")
        .returning(Agendamento)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    return [criar_evento(expirado, "expirado") for expirado in expirados]


async def processar_mensagem(
    fabrica: async_sessionmaker[AsyncSession],
    canal: AbstractChannel,
    mensagem: AbstractIncomingMessage,
) -> None:
    try:
        corpo = HoldSegurado.model_validate_json(mensagem.body)
        async with fabrica() as sessao:
            evento = await expirar_hold(sessao, corpo.hold_id)
            await sessao.commit()
    except Exception:
        # Sem voltar à fila: a agenda já trata hold vencido como livre, e a varredura o marca.
        logger.exception("Mensagem de holds.expirar enviada para a DLQ")
        await mensagem.reject(requeue=False)
    else:
        # Fora do try: o hold já expirou, e um evento perdido não é motivo para ir à DLQ.
        if evento is not None:
            await publicar_eventos(canal, [evento])
        await mensagem.ack()
