from dataclasses import asdict
from typing import Any
from uuid import UUID

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from clinica_core.embeddings import GeradorEmbeddings
from clinica_core.modelos import Chamada
from clinica_mcp_conhecimento.busca import MAXIMO_TRECHOS, buscar

_CABECALHO_CHAMADA = "x-chamada-id"
_INSTRUCOES = (
    "Base de conhecimento da clínica. Responda só com o que vier nos trechos: "
    "se eles não trouxerem a informação, diga que não sabe e ofereça transferir."
)


def _chamada_do_cabecalho(ctx: Context) -> UUID:
    bruto = (ctx.headers or {}).get(_CABECALHO_CHAMADA)
    if not bruto:
        raise ToolError("Conexão sem o header X-Chamada-Id")
    try:
        return UUID(bruto)
    except ValueError as erro:
        raise ToolError("Header X-Chamada-Id inválido") from erro


def criar_servidor(engine: AsyncEngine, gerador: GeradorEmbeddings) -> MCPServer:
    # Somente leitura também no banco, e não só no contrato da ferramenta.
    fabrica: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine.execution_options(postgresql_readonly=True), expire_on_commit=False
    )
    mcp: MCPServer = MCPServer("mcp-conhecimento", instructions=_INSTRUCOES)

    @mcp.tool()
    async def buscar_conhecimento(
        pergunta: str, ctx: Context, k: int = MAXIMO_TRECHOS
    ) -> dict[str, Any]:
        """Busca na base da clínica: procedimentos, preços, convênios, endereço e preparo.

        Devolve até 3 trechos com texto e fonte. Use só o que estiver neles para responder: nunca
        invente preço ou cobertura. Se não houver a informação, diga que não sabe e ofereça
        transferir para a recepção.
        """
        chamada_id = _chamada_do_cabecalho(ctx)
        async with fabrica() as sessao:
            clinica_id = await sessao.scalar(
                select(Chamada.clinica_id).where(Chamada.id == chamada_id)
            )
            if clinica_id is None:
                return {"ok": False, "motivo": "chamada_nao_encontrada", "trechos": []}
            trechos = await buscar(sessao, gerador, clinica_id, pergunta, k)
        return {"ok": True, "trechos": [asdict(trecho) for trecho in trechos]}

    @mcp.custom_route("/saude", methods=["GET"])  # type: ignore[untyped-decorator]
    async def saude(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return mcp


def criar_app(engine: AsyncEngine, gerador: GeradorEmbeddings) -> Starlette:
    # host fora de loopback: a proteção de DNS rebinding recusaria o nome do serviço no Compose.
    return criar_servidor(engine, gerador).streamable_http_app(
        stateless_http=True, json_response=True, host="0.0.0.0"
    )
