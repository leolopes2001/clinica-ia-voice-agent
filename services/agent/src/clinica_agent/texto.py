"""Agente em modo texto, só para testar os MCPs com `adk web` (T2.5). O de voz vem na Fase 3."""

from datetime import date
from uuid import UUID

import httpx
from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from pydantic_settings import BaseSettings, SettingsConfigDict

CHAVE_CHAMADA = "chamada_id"
_INSTRUCAO = """
Você testa as ferramentas da recepção de uma clínica odontológica, em português do Brasil.
A data de hoje é {data_de_hoje}.
Comece chamando identificar_chamador. Para tirar dúvidas, use só buscar_conhecimento.
Para agendar: listar_horarios_livres, segurar_horario e, depois de um sim explícito do usuário,
confirmar_agendamento. Remarcar e cancelar exigem verificar_identidade antes.
Nunca invente dados: se a ferramenta não trouxer a informação, diga que não sabe.
""".strip()


class ConfigAgenteTexto(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTE_TEXTO_", env_file=".env", extra="ignore")

    clinica_id: UUID | None = None
    # Fictício, o mesmo do seed. Troque para testar outro chamador.
    telefone: str = "+5511900000001"
    api_url: str = "http://localhost:8000"
    mcp_clinica_url: str = "http://localhost:8101/mcp"
    mcp_conhecimento_url: str = "http://localhost:8102/mcp"
    modelo: str = "gemini-flash-latest"


def cabecalhos_da_chamada(contexto: ReadonlyContext) -> dict[str, str]:
    chamada_id = contexto.state.get(CHAVE_CHAMADA)
    return {"X-Chamada-Id": chamada_id} if chamada_id else {}


async def criar_chamada(config: ConfigAgenteTexto, sessao_id: str) -> str:
    if config.clinica_id is None:
        raise RuntimeError("Configure AGENTE_TEXTO_CLINICA_ID com o valor impresso pelo seed")
    async with httpx.AsyncClient(base_url=config.api_url, timeout=5.0) as api:
        resposta = await api.post(
            "/chamadas",
            headers={"X-Clinica-Id": str(config.clinica_id), "Idempotency-Key": sessao_id},
            json={"telefone": config.telefone, "sala": f"texto-{sessao_id}"},
        )
    resposta.raise_for_status()
    chamada_id: str = resposta.json()["chamada_id"]
    return chamada_id


def criar_agente(config: ConfigAgenteTexto | None = None) -> LlmAgent:
    configuracao = config or ConfigAgenteTexto()

    async def abrir_chamada(callback_context: CallbackContext) -> None:
        if CHAVE_CHAMADA not in callback_context.state:
            callback_context.state[CHAVE_CHAMADA] = await criar_chamada(
                configuracao, callback_context.session.id
            )

    def toolset(url: str) -> McpToolset:
        return McpToolset(
            connection_params=StreamableHTTPConnectionParams(url=url),
            header_provider=cabecalhos_da_chamada,
        )

    def instrucao(_: ReadonlyContext) -> str:
        return _INSTRUCAO.format(data_de_hoje=date.today().isoformat())

    return LlmAgent(
        name="recepcionista_texto",
        model=configuracao.modelo,
        instruction=instrucao,
        before_agent_callback=abrir_chamada,
        tools=[
            toolset(configuracao.mcp_clinica_url),
            toolset(configuracao.mcp_conhecimento_url),
        ],
    )
