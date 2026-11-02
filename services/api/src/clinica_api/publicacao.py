import asyncio
import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from aio_pika.abc import AbstractChannel, AbstractRobustConnection

from clinica_core.config import ConfigRabbit
from clinica_core.eventos import EventoAgendamento, publicar_eventos
from clinica_core.mensageria import (
    FILA_HOLDS_ATRASO,
    LIMITE_PUBLICACAO_S,
    HoldSegurado,
    abrir_canal,
    conectar,
    declarar_topologia,
    publicar,
)

logger = logging.getLogger(__name__)

_ESPERA_RECONEXAO_S = 5.0


class PublicadorAgenda(Protocol):
    async def abrir(self) -> None: ...

    async def fechar(self) -> None: ...

    async def hold_criado(self, hold_id: UUID, duracao: timedelta) -> None: ...

    async def eventos(self, eventos: Sequence[EventoAgendamento]) -> None: ...


class PublicadorRabbit:
    """Nunca derruba o agendamento: o horário é liberado de qualquer forma quando o hold vence."""

    def __init__(self, config: ConfigRabbit, limite_s: float = LIMITE_PUBLICACAO_S) -> None:
        self._config = config
        self._limite_s = limite_s
        self._tarefa: asyncio.Task[None] | None = None
        self._conexao: AbstractRobustConnection | None = None
        self._canal: AbstractChannel | None = None

    async def abrir(self) -> None:
        # Em segundo plano: com o broker fora do ar, a API sobe e continua atendendo.
        self._tarefa = asyncio.create_task(self._conectar())

    async def _conectar(self) -> None:
        while True:
            conexao: AbstractRobustConnection | None = None
            try:
                conexao = await conectar(self._config)
                canal = await abrir_canal(conexao)
                await declarar_topologia(canal)
            except Exception as erro:
                logger.warning("RabbitMQ indisponível, tentando de novo: %s", erro)
                if conexao is not None:
                    await conexao.close()
                await asyncio.sleep(_ESPERA_RECONEXAO_S)
            else:
                self._conexao, self._canal = conexao, canal
                return

    async def fechar(self) -> None:
        if self._tarefa is not None:
            self._tarefa.cancel()
            await asyncio.gather(self._tarefa, return_exceptions=True)
        if self._conexao is not None:
            await self._conexao.close()

    async def hold_criado(self, hold_id: UUID, duracao: timedelta) -> None:
        if self._canal is None:
            logger.warning("Hold %s não publicado: RabbitMQ não conectado", hold_id)
            return
        try:
            async with asyncio.timeout(self._limite_s):
                await publicar(
                    self._canal,
                    FILA_HOLDS_ATRASO,
                    HoldSegurado(hold_id=hold_id),
                    expiracao=duracao,
                )
        except Exception as erro:
            logger.warning("Hold %s não publicado: %r", hold_id, erro)

    async def eventos(self, eventos: Sequence[EventoAgendamento]) -> None:
        if not eventos:
            return
        if self._canal is None:
            logger.warning("%d eventos não publicados: RabbitMQ não conectado", len(eventos))
            return
        await publicar_eventos(self._canal, eventos, self._limite_s)
