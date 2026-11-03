"""Retenção LGPD e limpeza de idempotência, uma vez por dia. Repetir não muda nada."""

import logging
from dataclasses import dataclass
from datetime import timedelta

from aio_pika.abc import AbstractIncomingMessage
from sqlalchemy import CursorResult, delete, func, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clinica_core.config import ConfigRetencao
from clinica_core.modelos import Chamada, Idempotencia, PedidoRetorno

logger = logging.getLogger(__name__)

_VALIDADE_DA_IDEMPOTENCIA = timedelta(hours=24)


@dataclass(frozen=True)
class ResultadoDaManutencao:
    transcricoes: int
    telefones: int
    pedidos: int
    chaves: int


def _linhas(resultado: object) -> int:
    assert isinstance(resultado, CursorResult)
    return resultado.rowcount


async def aplicar_retencao(sessao: AsyncSession, retencao: ConfigRetencao) -> ResultadoDaManutencao:
    prazo = func.now() - timedelta(days=retencao.transcricao_dias)
    # A linha fica: `agendamentos.chamada_id` aponta para ela, e o desfecho não identifica ninguém.
    transcricoes = await sessao.execute(
        update(Chamada)
        .where(Chamada.apagar_em <= func.now())
        .values(transcricao=None, apagar_em=None)
    )
    # Vale para toda chamada: com consulta, sem transcrição e também a que nunca recebeu o fim.
    telefones = await sessao.execute(
        update(Chamada)
        .where(
            func.coalesce(Chamada.fim, Chamada.inicio) <= prazo,
            (Chamada.telefone.is_not(None)) | (Chamada.paciente_id.is_not(None)),
        )
        .values(telefone=None, paciente_id=None, identidade_verificada_em=None)
    )
    # Um pedido pendente ainda precisa do telefone para alguém retornar.
    pedidos = await sessao.execute(
        update(PedidoRetorno)
        .where(
            PedidoRetorno.status == "concluido",
            PedidoRetorno.criado_em <= prazo,
            (PedidoRetorno.telefone.is_not(None)) | (PedidoRetorno.motivo.is_not(None)),
        )
        .values(telefone=None, motivo=None)
    )
    chaves = await sessao.execute(
        delete(Idempotencia).where(Idempotencia.criado_em <= func.now() - _VALIDADE_DA_IDEMPOTENCIA)
    )
    return ResultadoDaManutencao(
        _linhas(transcricoes), _linhas(telefones), _linhas(pedidos), _linhas(chaves)
    )


async def processar_mensagem(
    fabrica: async_sessionmaker[AsyncSession],
    retencao: ConfigRetencao,
    mensagem: AbstractIncomingMessage,
) -> None:
    try:
        async with fabrica() as sessao:
            resultado = await aplicar_retencao(sessao, retencao)
            await sessao.commit()
    except Exception:
        logger.exception("Manutenção diária enviada para a DLQ")
        await mensagem.reject(requeue=False)
    else:
        logger.info("Manutenção diária: %s", resultado)
        await mensagem.ack()
