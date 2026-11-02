from datetime import datetime
from importlib.resources import files
from zoneinfo import ZoneInfo

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.integrations.livekit import LiveKitToolset  # type: ignore[attr-defined]
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

from clinica_agent.chamada import CHAVE_CHAMADA, ChamadaAberta
from clinica_agent.config import ConfigAgente

FUSO = ZoneInfo("America/Sao_Paulo")
_DIAS_DA_SEMANA = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)


def cabecalhos_da_chamada(contexto: ReadonlyContext) -> dict[str, str]:
    return {"X-Chamada-Id": str(contexto.state[CHAVE_CHAMADA])}


def montar_prompt(clinica: str, agora: datetime) -> str:
    modelo = files("clinica_agent").joinpath("prompts/recepcionista.md").read_text("utf-8")
    local = agora.astimezone(FUSO)
    hoje = f"{_DIAS_DA_SEMANA[local.weekday()]}, {local:%d/%m/%Y}, {local:%H:%M}"
    return modelo.replace("{clinica}", clinica).replace("{hoje}", hoje)


def criar_agente(config: ConfigAgente, chamada: ChamadaAberta) -> LlmAgent:
    def toolset(url: str) -> McpToolset:
        return McpToolset(
            connection_params=StreamableHTTPConnectionParams(url=url),
            header_provider=cabecalhos_da_chamada,
        )

    return LlmAgent(
        name="recepcionista",
        model=config.modelo_voz,
        # Função, e não texto: o ADK trataria as chaves do prompt como variáveis de estado.
        instruction=lambda _: montar_prompt(chamada.clinica_nome, datetime.now(FUSO)),
        tools=[
            toolset(config.mcp_clinica_url),
            toolset(config.mcp_conhecimento_url),
            LiveKitToolset(),
        ],
    )
