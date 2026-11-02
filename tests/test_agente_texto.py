import asyncio
import socket
import threading
from collections.abc import AsyncGenerator, Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
import uvicorn
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.tools.mcp_tool import McpToolset
from google.genai import types

from clinica_agent import texto
from clinica_agent.texto import ConfigAgenteTexto, cabecalhos_da_chamada, criar_agente
from clinica_mcp_clinica.servidor import criar_app


class _ModeloRoteirizado(BaseLlm):
    """Pede uma ferramenta e depois responde, sem gastar a API do Gemini."""

    ferramenta: str = "identificar_chamador"
    ferramentas_vistas: list[str] = []

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.ferramentas_vistas[:] = list(llm_request.tools_dict)
        ultima = llm_request.contents[-1].parts or []
        if any(parte.function_response is not None for parte in ultima):
            parte = types.Part(text="ok")
        else:
            parte = types.Part(function_call=types.FunctionCall(name=self.ferramenta, args={}))
        yield LlmResponse(content=types.Content(role="model", parts=[parte]))


class _Contexto:
    def __init__(self, estado: dict[str, Any]) -> None:
        self.state = estado


def _porta_livre() -> int:
    with socket.socket() as soquete:
        soquete.bind(("127.0.0.1", 0))
        porta: int = soquete.getsockname()[1]
    return porta


class _ApiFalsa:
    def __init__(self) -> None:
        self.cabecalhos: list[httpx.Headers] = []

    def __call__(self, pedido: httpx.Request) -> httpx.Response:
        self.cabecalhos.append(pedido.headers)
        return httpx.Response(200, json={"pacientes": []})


@pytest.fixture
def api_falsa() -> _ApiFalsa:
    return _ApiFalsa()


@pytest.fixture
def url_mcp(api_falsa: _ApiFalsa) -> Iterator[str]:
    porta = _porta_livre()
    api = httpx.AsyncClient(transport=httpx.MockTransport(api_falsa), base_url="http://api")
    servidor = uvicorn.Server(
        uvicorn.Config(criar_app(api), host="127.0.0.1", port=porta, log_level="warning")
    )
    linha = threading.Thread(target=servidor.run, daemon=True)
    linha.start()
    while not servidor.started:  # noqa: ASYNC110
        threading.Event().wait(0.01)
    yield f"http://127.0.0.1:{porta}/mcp"
    servidor.should_exit = True
    linha.join(timeout=5)


def test_cabecalho_vem_do_estado_da_sessao() -> None:
    assert cabecalhos_da_chamada(_Contexto({"chamada_id": "abc"})) == {  # type: ignore[arg-type]
        "X-Chamada-Id": "abc"
    }
    assert cabecalhos_da_chamada(_Contexto({})) == {}  # type: ignore[arg-type]


def test_agente_usa_os_dois_mcps_com_headers_dinamicos() -> None:
    agente = criar_agente(ConfigAgenteTexto(clinica_id=uuid4()))
    assert len(agente.tools) == 2
    for toolset in agente.tools:
        assert isinstance(toolset, McpToolset)
        assert toolset.header_provider is cabecalhos_da_chamada


async def test_sem_clinica_configurada_nao_abre_chamada() -> None:
    with pytest.raises(RuntimeError, match="AGENTE_TEXTO_CLINICA_ID"):
        await texto.criar_chamada(ConfigAgenteTexto(clinica_id=None), "sessao")


async def test_abrir_chamada_manda_telefone_do_contexto_e_chave_da_sessao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recebidos: list[httpx.Request] = []

    def api(pedido: httpx.Request) -> httpx.Response:
        recebidos.append(pedido)
        return httpx.Response(201, json={"chamada_id": "id-da-chamada"})

    cliente_real = httpx.AsyncClient

    def cliente_falso(**opcoes: Any) -> httpx.AsyncClient:
        return cliente_real(transport=httpx.MockTransport(api), **opcoes)

    monkeypatch.setattr(httpx, "AsyncClient", cliente_falso)
    clinica = uuid4()
    config = ConfigAgenteTexto(clinica_id=clinica, telefone="+5511900000009")
    assert await texto.criar_chamada(config, "sessao-1") == "id-da-chamada"
    assert recebidos[0].headers["X-Clinica-Id"] == str(clinica)
    assert recebidos[0].headers["Idempotency-Key"] == "sessao-1"
    assert b"+5511900000009" in recebidos[0].content


async def test_adk_chama_a_ferramenta_com_o_header_da_chamada(
    monkeypatch: pytest.MonkeyPatch, url_mcp: str, api_falsa: _ApiFalsa
) -> None:
    async def chamada_falsa(_: ConfigAgenteTexto, __: str) -> str:
        return "chamada-do-teste"

    monkeypatch.setattr(texto, "criar_chamada", chamada_falsa)
    modelo = _ModeloRoteirizado(model="roteirizado")
    config = ConfigAgenteTexto(
        clinica_id=uuid4(), mcp_clinica_url=url_mcp, mcp_conhecimento_url=url_mcp
    )
    agente = criar_agente(config)
    agente.model = modelo
    agente.tools = agente.tools[:1]
    runner = InMemoryRunner(agent=agente, app_name="teste")
    sessao = await runner.session_service.create_session(app_name="teste", user_id="u")
    try:
        mensagem = types.Content(role="user", parts=[types.Part(text="oi")])
        eventos = [
            evento
            async for evento in runner.run_async(
                user_id="u", session_id=sessao.id, new_message=mensagem
            )
        ]
    finally:
        await runner.close()
        await asyncio.sleep(0)

    assert [h["X-Chamada-Id"] for h in api_falsa.cabecalhos] == ["chamada-do-teste"]
    assert "identificar_chamador" in modelo.ferramentas_vistas
    assert len(modelo.ferramentas_vistas) == 12
    respostas = [e for e in eventos if e.get_function_responses()]
    assert respostas
