from http import HTTPStatus

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from clinica_painel.api import ErroDaApi, chamar
from clinica_painel.telas import pagina

router = APIRouter()


@router.get("/login")
async def ver_login(request: Request) -> Response:
    if "usuario_id" in request.session:
        return RedirectResponse("/", HTTPStatus.SEE_OTHER)
    return pagina(request, "login.html")


@router.post("/login")
async def entrar(request: Request, email: str = Form(), senha: str = Form()) -> Response:
    try:
        dados = await chamar(
            request, "POST", "/painel/login", json={"email": email, "senha": senha}
        )
    except ErroDaApi as erro:
        return pagina(request, "login.html", erro.status, erro=erro.mensagem, email=email)
    # Sessão nova a cada login. O perfil aqui só desenha o menu: quem decide é a API.
    request.session.clear()
    request.session.update(
        {
            "usuario_id": dados["usuario_id"],
            "clinica_id": dados["clinica_id"],
            "email": dados["email"],
            "perfil": dados["perfil"],
            "clinica": dados["clinica"],
            "fuso": dados["fuso"],
        }
    )
    return RedirectResponse("/", HTTPStatus.SEE_OTHER)


@router.post("/sair")
async def sair(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", HTTPStatus.SEE_OTHER)


@router.get("/minha-senha")
async def ver_minha_senha(request: Request) -> Response:
    return pagina(request, "minha_senha.html")


@router.post("/minha-senha")
async def trocar_minha_senha(request: Request, atual: str = Form(), nova: str = Form()) -> Response:
    try:
        await chamar(request, "POST", "/painel/minha-senha", json={"atual": atual, "nova": nova})
    except ErroDaApi as erro:
        if erro.status == HTTPStatus.UNAUTHORIZED:
            raise
        return pagina(request, "minha_senha.html", erro.status, erro=erro.mensagem)
    return pagina(request, "minha_senha.html", aviso="Senha alterada.")


@router.get("/auditoria")
async def ver_auditoria(request: Request) -> Response:
    registros = await chamar(request, "GET", "/painel/auditoria")
    return pagina(request, "auditoria.html", registros=registros)
