import asyncio
import logging
from uuid import UUID

from clinica_agent.resumo import ResumoDaLigacao
from clinica_core.config import ConfigRabbit
from clinica_core.mensageria import (
    FILA_CHAMADA_ENCERRADA,
    LIMITE_PUBLICACAO_S,
    ChamadaEncerrada,
    abrir_canal,
    conectar,
    declarar_topologia,
    publicar,
)

logger = logging.getLogger(__name__)


async def publicar_encerramento(
    config: ConfigRabbit, chamada_id: str, resumo: ResumoDaLigacao
) -> None:
    mensagem = ChamadaEncerrada(
        chamada_id=UUID(chamada_id), desfecho=resumo.desfecho, transcricao=resumo.transcricao
    )
    try:
        async with asyncio.timeout(LIMITE_PUBLICACAO_S):
            conexao = await conectar(config)
            try:
                canal = await abrir_canal(conexao)
                await declarar_topologia(canal)
                await publicar(canal, FILA_CHAMADA_ENCERRADA, mensagem)
            finally:
                await conexao.close()
    except Exception as erro:
        # Sem a transcrição no log. A chamada fica sem fim e sem desfecho, mas o paciente já saiu.
        logger.warning("Falha ao publicar o encerramento da chamada %s: %r", chamada_id, erro)
