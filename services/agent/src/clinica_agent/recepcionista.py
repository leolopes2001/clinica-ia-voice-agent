import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from importlib.resources import files
from typing import Any
from zoneinfo import ZoneInfo

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.integrations.livekit import LiveKitToolset  # type: ignore[attr-defined]
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

from clinica_agent.chamada import CHAVE_CHAMADA, ChamadaAberta
from clinica_agent.config import ConfigAgente
from clinica_agent.trava import TravaDeConfirmacao

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
# Tempo para o áudio da despedida tocar: o LiveKitRunner descarta o que falta ao desligar.
ESPERA_DA_DESPEDIDA_S = 4.0


class ToolsetEmSerie(McpToolset):
    """Executa uma ferramenta por vez, na ordem pedida pelo modelo.

    O modelo às vezes pede verificar_identidade e listar_meus_agendamentos no mesmo turno, e o ADK
    roda as duas juntas: a segunda chega à API antes de a primeira gravar, e é recusada.
    """

    def __init__(self, *, trava: asyncio.Lock, **argumentos: Any) -> None:
        super().__init__(**argumentos)
        self._trava = trava

    async def get_tools(self, readonly_context: ReadonlyContext | None = None) -> list[BaseTool]:
        ferramentas: list[BaseTool] = await super().get_tools(readonly_context)
        for ferramenta in ferramentas:
            _executar_em_serie(ferramenta, self._trava)
        return ferramentas


def _executar_em_serie(ferramenta: BaseTool, trava: asyncio.Lock) -> None:
    original: Callable[..., Awaitable[Any]] = ferramenta.run_async

    async def executar(**argumentos: Any) -> Any:
        async with trava:
            return await original(**argumentos)

    setattr(ferramenta, "run_async", executar)  # noqa: B010


class ToolsetDaLigacao(LiveKitToolset):
    """Deixa a despedida tocar até o fim antes de desligar."""

    async def get_tools(self, readonly_context: ReadonlyContext | None = None) -> list[BaseTool]:
        ferramentas: list[BaseTool] = await super().get_tools(readonly_context)
        for ferramenta in ferramentas:
            if ferramenta.name == "end_call":
                _esperar_antes(ferramenta, ESPERA_DA_DESPEDIDA_S)
        return ferramentas


def _esperar_antes(ferramenta: BaseTool, segundos: float) -> None:
    original: Callable[..., Awaitable[Any]] = ferramenta.run_async

    async def executar(**argumentos: Any) -> Any:
        await asyncio.sleep(segundos)
        return await original(**argumentos)

    setattr(ferramenta, "run_async", executar)  # noqa: B010


def cabecalhos_da_chamada(contexto: ReadonlyContext) -> dict[str, str]:
    return {"X-Chamada-Id": str(contexto.state[CHAVE_CHAMADA])}


def montar_prompt(clinica: str, agora: datetime) -> str:
    modelo = files("clinica_agent").joinpath("prompts/recepcionista.md").read_text("utf-8")
    local = agora.astimezone(FUSO)
    hoje = f"{_DIAS_DA_SEMANA[local.weekday()]}, {local:%d/%m/%Y}, {local:%H:%M}"
    return modelo.replace("{clinica}", clinica).replace("{hoje}", hoje)


def criar_agente(
    config: ConfigAgente, chamada: ChamadaAberta, trava_de_confirmacao: TravaDeConfirmacao
) -> LlmAgent:
    # Uma por ligação: o processo atende uma chamada, e as chamadas não esperam umas pelas outras.
    trava = asyncio.Lock()

    def toolset(url: str) -> McpToolset:
        return ToolsetEmSerie(
            trava=trava,
            connection_params=StreamableHTTPConnectionParams(url=url),
            header_provider=cabecalhos_da_chamada,
        )

    return LlmAgent(
        name="recepcionista",
        model=config.modelo_voz,
        # Função, e não texto: o ADK trataria as chaves do prompt como variáveis de estado.
        instruction=lambda _: montar_prompt(chamada.clinica_nome, datetime.now(FUSO)),
        before_tool_callback=trava_de_confirmacao.antes_da_ferramenta,
        after_tool_callback=trava_de_confirmacao.depois_da_ferramenta,
        tools=[
            toolset(config.mcp_clinica_url),
            toolset(config.mcp_conhecimento_url),
            ToolsetDaLigacao(),
        ],
    )
