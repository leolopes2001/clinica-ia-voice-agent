import logging
from uuid import UUID

from aio_pika.abc import AbstractIncomingMessage
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clinica_core.mensageria import HoldSegurado
from clinica_core.modelos import Agendamento

logger = logging.getLogger(__name__)


async def expirar_hold(sessao: AsyncSession, hold_id: UUID) -> bool:
    expirado = await sessao.scalar(
        update(Agendamento)
        .where(
            Agendamento.id == hold_id,
            Agendamento.status == "segurado",
            Agendamento.expira_em <= func.now(),
        )
        .values(status="expirado")
        .returning(Agendamento.id)
    )
    return expirado is not None


async def expirar_vencidos(sessao: AsyncSession) -> list[UUID]:
    # SKIP LOCKED: a varredura nunca espera uma transação de segurar, então não forma deadlock.
    vencidos = (
        select(Agendamento.id)
        .where(Agendamento.status == "segurado", Agendamento.expira_em <= func.now())
        .with_for_update(skip_locked=True)
    )
    resultado = await sessao.scalars(
        update(Agendamento)
        .where(Agendamento.id.in_(vencidos))
        .values(status="expirado")
        .returning(Agendamento.id)
    )
    return list(resultado)


async def processar_mensagem(
    fabrica: async_sessionmaker[AsyncSession], mensagem: AbstractIncomingMessage
) -> None:
    try:
        corpo = HoldSegurado.model_validate_json(mensagem.body)
        async with fabrica() as sessao:
            await expirar_hold(sessao, corpo.hold_id)
            await sessao.commit()
    except Exception:
        # Sem voltar à fila: a agenda já trata hold vencido como livre, e a varredura o marca.
        logger.exception("Mensagem de holds.expirar enviada para a DLQ")
        await mensagem.reject(requeue=False)
    else:
        await mensagem.ack()
