"""Eventos da agenda para o SSE. São só um aviso: a página relê a agenda da API."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection

from clinica_core.config import ConfigRabbit
from clinica_core.eventos import EventoAgendamento
from clinica_core.mensageria import (
    abrir_canal,
    conectar,
    declarar_fila_painel,
    declarar_topologia,
)

logger = logging.getLogger(__name__)

_ESPERA_RECONEXAO_S = 5.0
_FILA_DE_CADA_NAVEGADOR = 100


class FonteDeEventos(Protocol):
    async def abrir(self) -> None: ...

    async def fechar(self) -> None: ...

    def assinar(self, clinica_id: str) -> "asyncio.Queue[str]": ...

    def cancelar(self, fila: "asyncio.Queue[str]") -> None: ...


class Distribuidor:
    """Entrega cada evento às páginas abertas da clínica, sem dado de paciente."""

    def __init__(self) -> None:
        self._filas: dict[asyncio.Queue[str], str] = {}

    async def abrir(self) -> None:
        pass

    async def fechar(self) -> None:
        pass

    def assinar(self, clinica_id: str) -> "asyncio.Queue[str]":
        fila: asyncio.Queue[str] = asyncio.Queue(_FILA_DE_CADA_NAVEGADOR)
        self._filas[fila] = clinica_id
        return fila

    def cancelar(self, fila: "asyncio.Queue[str]") -> None:
        self._filas.pop(fila, None)

    def distribuir(self, evento: EventoAgendamento) -> None:
        for fila, clinica_id in self._filas.items():
            if clinica_id == str(evento.clinica_id):
                # Fila cheia: a página já tem avisos de sobra para reler a agenda.
                with contextlib.suppress(asyncio.QueueFull):
                    fila.put_nowait(evento.tipo)


class FonteDeEventosRabbit(Distribuidor):
    def __init__(self, config: ConfigRabbit) -> None:
        super().__init__()
        self._config = config
        self._tarefa: asyncio.Task[None] | None = None
        self._conexao: AbstractRobustConnection | None = None

    async def abrir(self) -> None:
        # Em segundo plano: sem o broker, o painel sobe, e a página relê a agenda periodicamente.
        self._tarefa = asyncio.create_task(self._consumir())

    async def _consumir(self) -> None:
        while True:
            try:
                self._conexao = await conectar(self._config)
                canal = await abrir_canal(self._conexao)
                await declarar_topologia(canal)
                fila = await declarar_fila_painel(canal)
                await fila.consume(self._receber)
                return
            except Exception as erro:
                logger.warning("RabbitMQ indisponível, tentando de novo: %s", erro)
                if self._conexao is not None:
                    await self._conexao.close()
                    self._conexao = None
                await asyncio.sleep(_ESPERA_RECONEXAO_S)

    async def _receber(self, mensagem: AbstractIncomingMessage) -> None:
        async with mensagem.process(requeue=False):
            self.distribuir(EventoAgendamento.model_validate_json(mensagem.body))

    async def fechar(self) -> None:
        if self._tarefa is not None:
            self._tarefa.cancel()
            await asyncio.gather(self._tarefa, return_exceptions=True)
        if self._conexao is not None:
            await self._conexao.close()


@asynccontextmanager
async def assinatura(fonte: FonteDeEventos, clinica_id: str) -> AsyncIterator["asyncio.Queue[str]"]:
    fila = fonte.assinar(clinica_id)
    try:
        yield fila
    finally:
        fonte.cancelar(fila)
