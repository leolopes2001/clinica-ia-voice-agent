import asyncio
import logging
import signal
from pathlib import Path

from aio_pika.abc import AbstractChannel, AbstractIncomingMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clinica_core.config import ConfigConhecimento, ConfigEmbeddings, ConfigPostgres, ConfigRabbit
from clinica_core.db import criar_engine, criar_fabrica_sessoes
from clinica_core.embeddings import GeminiEmbeddings, GeradorEmbeddings
from clinica_core.eventos import publicar_eventos
from clinica_core.mensageria import (
    FILA_CONHECIMENTO_INDEXAR,
    FILA_HOLDS_EXPIRAR,
    abrir_canal,
    conectar,
    declarar_topologia,
)
from clinica_worker import conhecimento
from clinica_worker.holds import expirar_vencidos, processar_mensagem

logger = logging.getLogger(__name__)

ARQUIVO_VIVO = Path("/tmp/clinica-worker-vivo")
INTERVALO_VARREDURA_S = 60.0


async def _varrer(
    fabrica: async_sessionmaker[AsyncSession], canal: AbstractChannel, arquivo_vivo: Path
) -> None:
    try:
        async with fabrica() as sessao:
            expirados = await expirar_vencidos(sessao)
            await sessao.commit()
    except Exception:
        logger.exception("Falha na varredura de holds vencidos")
        return
    if expirados:
        logger.info("Varredura expirou %d holds", len(expirados))
        await publicar_eventos(canal, expirados)
    # Só depois de falar com o banco: o health check do worker depende deste arquivo.
    await asyncio.to_thread(arquivo_vivo.touch)


async def rodar(
    config_postgres: ConfigPostgres,
    config_rabbit: ConfigRabbit,
    parar: asyncio.Event,
    intervalo_s: float = INTERVALO_VARREDURA_S,
    arquivo_vivo: Path = ARQUIVO_VIVO,
    gerador: GeradorEmbeddings | None = None,
    config_conhecimento: ConfigConhecimento | None = None,
) -> None:
    engine = criar_engine(config_postgres)
    fabrica = criar_fabrica_sessoes(engine)
    conexao = await conectar(config_rabbit)
    gerador_embeddings = gerador or GeminiEmbeddings(ConfigEmbeddings())
    diretorio = (config_conhecimento or ConfigConhecimento()).dir

    try:
        canal = await abrir_canal(conexao)

        async def consumir(mensagem: AbstractIncomingMessage) -> None:
            await processar_mensagem(fabrica, canal, mensagem)

        await canal.set_qos(prefetch_count=10)
        await declarar_topologia(canal)
        fila = await canal.get_queue(FILA_HOLDS_EXPIRAR)
        await fila.consume(consumir)

        async def consumir_indexacao(mensagem: AbstractIncomingMessage) -> None:
            await conhecimento.processar_mensagem(fabrica, gerador_embeddings, diretorio, mensagem)

        # Canal próprio: um documento por vez, sem atrasar a expiração dos holds.
        canal_indexacao = await abrir_canal(conexao)
        await canal_indexacao.set_qos(prefetch_count=1)
        fila_indexacao = await canal_indexacao.get_queue(FILA_CONHECIMENTO_INDEXAR)
        await fila_indexacao.consume(consumir_indexacao)
        while not parar.is_set():
            await _varrer(fabrica, canal, arquivo_vivo)
            try:
                async with asyncio.timeout(intervalo_s):
                    await parar.wait()
            except TimeoutError:
                pass
    finally:
        await conexao.close()
        await engine.dispose()


async def _principal() -> None:
    logging.basicConfig(level=logging.INFO)
    parar = asyncio.Event()
    laco = asyncio.get_running_loop()
    for sinal in (signal.SIGTERM, signal.SIGINT):
        laco.add_signal_handler(sinal, parar.set)
    await rodar(ConfigPostgres(), ConfigRabbit(), parar)


def main() -> None:
    asyncio.run(_principal())
