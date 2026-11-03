import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from clinica_painel import agenda, cadastros, contas
from clinica_painel.api import ErroDaApi
from clinica_painel.config import ConfigPainel
from clinica_painel.eventos import Distribuidor, FonteDeEventos
from clinica_painel.telas import pagina

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).resolve().parent
_PUBLICAS = ("/login", "/static/", "/saude")
_METODOS_DE_ESCRITA = {"POST", "PUT", "PATCH", "DELETE"}


class _MesmaOrigem:
    """Com o cookie SameSite=Strict, é a segunda barreira contra um formulário de outro site."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in _METODOS_DE_ESCRITA:
            cabecalhos = {k.decode(): v.decode() for k, v in scope["headers"]}
            origem = cabecalhos.get("origin")
            if origem and urlsplit(origem).netloc != cabecalhos.get("host"):
                resposta = JSONResponse({"motivo": "origem_recusada"}, HTTPStatus.FORBIDDEN)
                await resposta(scope, receive, send)
                return
        await self._app(scope, receive, send)


class _ExigirLogin:
    """Em ASGI puro: o BaseHTTPMiddleware atrapalha o SSE, que precisa notar quem desconectou."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        caminho: str = scope["path"]
        if not caminho.startswith(_PUBLICAS) and "usuario_id" not in scope["session"]:
            await RedirectResponse("/login", HTTPStatus.SEE_OTHER)(scope, receive, send)
            return

        async def enviar(mensagem: Message) -> None:
            if mensagem["type"] == "http.response.start":
                cabecalhos = MutableHeaders(scope=mensagem)
                cabecalhos["X-Frame-Options"] = "DENY"
                cabecalhos["X-Content-Type-Options"] = "nosniff"
                cabecalhos["Referrer-Policy"] = "same-origin"
                # Dados de saúde não ficam no cache do navegador da recepção.
                if not caminho.startswith("/static/"):
                    cabecalhos["Cache-Control"] = "no-store"
            await send(mensagem)

        await self._app(scope, receive, enviar)


def criar_app(
    api: httpx.AsyncClient,
    busca: httpx.AsyncClient,
    config: ConfigPainel | None = None,
    eventos: FonteDeEventos | None = None,
) -> FastAPI:
    configuracao = config or ConfigPainel()
    fonte = eventos or Distribuidor()

    @asynccontextmanager
    async def ciclo_de_vida(_: FastAPI) -> AsyncIterator[None]:
        await fonte.abrir()
        yield
        await fonte.fechar()
        await api.aclose()
        await busca.aclose()

    app = FastAPI(
        title="Clínica Voz: painel", lifespan=ciclo_de_vida, docs_url=None, redoc_url=None
    )
    app.state.api = api
    app.state.busca = busca
    app.state.config = configuracao
    app.state.eventos = fonte

    # O último adicionado roda primeiro: a sessão vem antes do login.
    app.add_middleware(_ExigirLogin)
    app.add_middleware(_MesmaOrigem)
    app.add_middleware(
        SessionMiddleware,
        secret_key=configuracao.segredo_sessao.get_secret_value(),
        session_cookie="clinica_sessao",
        max_age=configuracao.horas_de_sessao * 3600,
        same_site="strict",
        https_only=configuracao.cookie_seguro,
    )

    @app.exception_handler(ErroDaApi)
    async def erro_da_api(request: Request, erro: ErroDaApi) -> Response:
        if erro.status == HTTPStatus.UNAUTHORIZED:
            request.session.clear()
            return RedirectResponse("/login", HTTPStatus.SEE_OTHER)
        return pagina(request, "erro.html", erro.status, mensagem=erro.mensagem)

    app.mount("/static", StaticFiles(directory=_RAIZ / "static"), name="static")
    app.include_router(contas.router)
    app.include_router(agenda.router)
    app.include_router(cadastros.router)

    @app.get("/saude")
    async def saude() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def inicio() -> RedirectResponse:
        return RedirectResponse("/agenda", HTTPStatus.SEE_OTHER)

    return app
