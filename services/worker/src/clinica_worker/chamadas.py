import logging
from datetime import timedelta

from aio_pika.abc import AbstractIncomingMessage
from sqlalchemy import case, func, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clinica_core.config import ConfigRetencao
from clinica_core.mensageria import ChamadaEncerrada
from clinica_core.modelos import Chamada

logger = logging.getLogger(__name__)


async def gravar_encerramento(
    sessao: AsyncSession, encerrada: ChamadaEncerrada, retencao: ConfigRetencao
) -> None:
    # Quem decide é o banco: sem consentimento dado na chamada, a transcrição não é guardada.
    com_consentimento = Chamada.consentimento_em.is_not(None)
    prazo = func.now() + timedelta(days=retencao.transcricao_dias)
    guardar = encerrada.transcricao is not None
    await sessao.execute(
        update(Chamada)
        .where(Chamada.id == encerrada.chamada_id, Chamada.fim.is_(None))
        .values(
            fim=func.now(),
            desfecho=encerrada.desfecho,
            transcricao=case((com_consentimento, encerrada.transcricao), else_=None),
            apagar_em=case((com_consentimento, prazo), else_=None) if guardar else None,
        )
    )


async def processar_mensagem(
    fabrica: async_sessionmaker[AsyncSession],
    retencao: ConfigRetencao,
    mensagem: AbstractIncomingMessage,
) -> None:
    try:
        encerrada = ChamadaEncerrada.model_validate_json(mensagem.body)
        async with fabrica() as sessao:
            await gravar_encerramento(sessao, encerrada, retencao)
            await sessao.commit()
    except Exception:
        # Sem voltar à fila. O corpo não vai ao log: pode ter dado de saúde.
        logger.exception("Mensagem de chamada.encerrada enviada para a DLQ")
        await mensagem.reject(requeue=False)
    else:
        await mensagem.ack()
