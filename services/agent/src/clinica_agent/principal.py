import logging

import httpx
from google.adk.events import Event
from google.adk.integrations.livekit import LiveKitRunner  # type: ignore[attr-defined]
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, InMemorySessionService
from google.genai import types
from livekit import rtc
from livekit.agents import AgentServer, AutoSubscribe, JobContext, cli

from clinica_agent.chamada import CHAVE_CHAMADA, abrir_chamada, telefone_do_chamador
from clinica_agent.config import ConfigAgente
from clinica_agent.encerramento import publicar_encerramento
from clinica_agent.recepcionista import criar_agente
from clinica_agent.resumo import resumir
from clinica_core.config import ConfigRabbit

logger = logging.getLogger(__name__)

NOME_AGENTE = "recepcionista"
APP = "recepcionista"
USUARIO_ADK = "chamador"
# O modelo só fala depois de uma fala do usuário, e quem liga espera ouvir a clínica primeiro.
_LIGACAO_ATENDIDA = "[A ligação acabou de ser atendida. Fale agora a sua abertura.]"
PROCESSOS_OCIOSOS = 2

server = AgentServer(num_idle_processes=PROCESSOS_OCIOSOS)


async def _preparar_sessao(servico: BaseSessionService, sessao_id: str, chamada_id: str) -> None:
    sessao = await servico.get_session(app_name=APP, user_id=USUARIO_ADK, session_id=sessao_id)
    if sessao is not None:
        return
    sessao = await servico.create_session(
        app_name=APP,
        user_id=USUARIO_ADK,
        session_id=sessao_id,
        state={CHAVE_CHAMADA: chamada_id},
    )
    await servico.append_event(
        sessao,
        Event(
            author="user",
            content=types.Content(role="user", parts=[types.Part(text=_LIGACAO_ATENDIDA)]),
        ),
    )


async def _encerrar(servico: BaseSessionService, sessao_id: str, chamada_id: str) -> None:
    sessao = await servico.get_session(app_name=APP, user_id=USUARIO_ADK, session_id=sessao_id)
    if sessao is not None:
        await publicar_encerramento(ConfigRabbit(), chamada_id, resumir(sessao.events))


@server.rtc_session(agent_name=NOME_AGENTE)
async def atender(ctx: JobContext) -> None:
    config = ConfigAgente()
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    participante = await ctx.wait_for_participant()
    telefone = telefone_do_chamador(
        dict(participante.attributes),
        participante.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
        ctx.job.metadata,
        config.permitir_telefone_de_teste,
    )
    async with httpx.AsyncClient(base_url=config.api_url, timeout=5.0) as api:
        chamada = await abrir_chamada(api, config.clinica_id, telefone, ctx.room.name)

    servico: BaseSessionService = InMemorySessionService()
    await _preparar_sessao(servico, ctx.room.name, chamada.chamada_id)
    runner = Runner(agent=criar_agente(config, chamada), app_name=APP, session_service=servico)
    try:
        await LiveKitRunner(
            runner=runner,
            room=ctx.room,
            user_id=USUARIO_ADK,
            session_id=ctx.room.name,
            create_session=False,
        ).start()
    finally:
        await _encerrar(servico, ctx.room.name, chamada.chamada_id)
        ctx.shutdown("ligação encerrada")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cli.run_app(server)
