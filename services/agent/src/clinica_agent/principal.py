import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from google.adk.agents.run_config import RunConfig
from google.adk.events import Event
from google.adk.integrations.livekit import LiveKitRunner  # type: ignore[attr-defined]
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, DatabaseSessionService
from google.genai import types
from livekit import rtc
from livekit.agents import AgentServer, AutoSubscribe, JobContext, cli

from clinica_agent.chamada import CHAVE_CHAMADA, abrir_chamada, telefone_do_chamador
from clinica_agent.config import ConfigAgente
from clinica_agent.encerramento import publicar_encerramento
from clinica_agent.espera import FraseDeEspera, publicar_frase_de_espera
from clinica_agent.recepcionista import criar_agente
from clinica_agent.resumo import resumir
from clinica_agent.trava import TravaDeConfirmacao
from clinica_core.config import ConfigRabbit, ConfigSessoesAdk, ConfigTokens

logger = logging.getLogger(__name__)

NOME_AGENTE = "recepcionista"
APP = "recepcionista"
USUARIO_ADK = "chamador"
# O modelo só fala depois de uma fala do usuário, e quem liga espera ouvir a clínica primeiro.
_LIGACAO_ATENDIDA = "[A ligação acabou de ser atendida. Fale agora a sua abertura.]"
PROCESSOS_OCIOSOS = 2
# Uma ligação não dura tanto: o que passou disso é de um processo que morreu no meio.
_IDADE_DE_SESSAO_ABANDONADA = timedelta(days=1)


class RunnerObservado(Runner):
    """Entrega cada evento ao vivo, inclusive os parciais, que não entram na sessão."""

    def __init__(
        self,
        *,
        trava_de_confirmacao: TravaDeConfirmacao,
        frase_de_espera: FraseDeEspera | None = None,
        **argumentos: Any,
    ) -> None:
        super().__init__(**argumentos)
        self._trava_de_confirmacao = trava_de_confirmacao
        self._frase_de_espera = frase_de_espera

    async def run_live(self, *args: Any, **argumentos: Any) -> AsyncGenerator[Event, None]:
        async with contextlib.aclosing(super().run_live(*args, **argumentos)) as eventos:
            async for evento in eventos:
                self._trava_de_confirmacao.observar(evento)
                if self._frase_de_espera is not None:
                    self._frase_de_espera.observar(evento)
                yield evento


def config_de_voz(config: ConfigAgente, voz: str | None = None) -> RunConfig:
    # Sem voz escolhida no painel, fica a padrão do modelo.
    voz_escolhida = (
        types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voz))
        if voz
        else None
    )
    return RunConfig(
        speech_config=types.SpeechConfig(language_code="pt-BR", voice_config=voz_escolhida),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                silence_duration_ms=config.silencio_fim_de_fala_ms
            )
        ),
    )


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


async def encerrar_sessao(
    servico: BaseSessionService, sessao_id: str, chamada_id: str, config_rabbit: ConfigRabbit
) -> None:
    sessao = await servico.get_session(app_name=APP, user_id=USUARIO_ADK, session_id=sessao_id)
    if sessao is None:
        return
    try:
        await publicar_encerramento(config_rabbit, chamada_id, resumir(sessao.events))
    finally:
        # Os eventos têm a conversa e os dados do paciente: a retenção só vale para `chamadas`.
        await servico.delete_session(app_name=APP, user_id=USUARIO_ADK, session_id=sessao_id)


async def apagar_sessoes_abandonadas(servico: BaseSessionService, agora: datetime) -> int:
    resposta = await servico.list_sessions(app_name=APP, user_id=USUARIO_ADK)
    limite = (agora - _IDADE_DE_SESSAO_ABANDONADA).timestamp()
    abandonadas = [s for s in resposta.sessions if s.last_update_time < limite]
    for sessao in abandonadas:
        await servico.delete_session(app_name=APP, user_id=USUARIO_ADK, session_id=sessao.id)
    return len(abandonadas)


async def _preparar_banco_de_sessoes() -> None:
    # Antes de qualquer ligação: dois jobs criando as tabelas ao mesmo tempo dariam erro.
    servico = DatabaseSessionService(ConfigSessoesAdk().url)
    try:
        await servico.prepare_tables()
        apagadas = await apagar_sessoes_abandonadas(servico, datetime.now(UTC))
        if apagadas:
            logger.warning("%d sessões abandonadas apagadas", apagadas)
    finally:
        await servico.close()


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
    async with httpx.AsyncClient(
        base_url=config.api_url, timeout=5.0, headers=ConfigTokens().cabecalho_servicos
    ) as api:
        chamada = await abrir_chamada(api, config.clinica_id, telefone, ctx.room.name)

    servico = DatabaseSessionService(ConfigSessoesAdk().url)
    await _preparar_sessao(servico, ctx.room.name, chamada.chamada_id)
    trava = TravaDeConfirmacao()
    frase = await publicar_frase_de_espera(ctx.room, config.frase_espera_arquivo)
    runner = RunnerObservado(
        trava_de_confirmacao=trava,
        frase_de_espera=frase,
        agent=criar_agente(config, chamada, trava, frase),
        app_name=APP,
        session_service=servico,
    )
    try:
        await LiveKitRunner(
            runner=runner,
            room=ctx.room,
            user_id=USUARIO_ADK,
            session_id=ctx.room.name,
            create_session=False,
            run_config=config_de_voz(config, chamada.configuracao.voz),
        ).start()
    finally:
        if frase is not None:
            await frase.fechar()
        try:
            await encerrar_sessao(servico, ctx.room.name, chamada.chamada_id, ConfigRabbit())
        finally:
            await servico.close()
            ctx.shutdown("ligação encerrada")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_preparar_banco_de_sessoes())
    cli.run_app(server)
