"""Telas de cadastro, todas iguais: a tabela, o formulário e as chamadas à API."""

from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from clinica_painel.api import ErroDaApi, chamar
from clinica_painel.formularios import Cadastro, Campo, ler_formulario, opcoes_dos_campos
from clinica_painel.telas import pagina

router = APIRouter()

CADASTROS: dict[str, Cadastro] = {}


def registrar(cadastro: Cadastro) -> None:
    CADASTROS[cadastro.rota] = cadastro


registrar(
    Cadastro(
        rota="usuarios",
        titulo="Usuários",
        singular="usuário",
        caminho_api="/painel/usuarios",
        pode_apagar=False,
        campos=[
            Campo("email", "E-mail", "email"),
            Campo(
                "perfil",
                "Perfil",
                "select",
                opcoes=lambda _: _perfis(),
                na_tabela=True,
            ),
            Campo("ativo", "Ativo", "checkbox", obrigatorio=False, so_ao_editar=True),
            Campo(
                "senha",
                "Senha",
                "password",
                obrigatorio=False,
                na_tabela=False,
                ajuda="Pelo menos 10 caracteres. Ao editar, deixe vazia para manter.",
            ),
        ],
    )
)


registrar(
    Cadastro(
        rota="pacientes",
        titulo="Pacientes",
        singular="paciente",
        caminho_api="/painel/pacientes",
        so_admin=False,
        pode_apagar=False,
        filtro_de_busca=True,
        campos=[
            Campo("nome", "Nome completo"),
            Campo("telefone_e164", "Telefone", ajuda="Com DDD, como 11 98888-7777."),
            Campo("data_nascimento", "Data de nascimento", "date"),
        ],
    )
)


async def _perfis() -> list[tuple[str, str]]:
    return [("recepcao", "Recepção"), ("admin", "Administrador")]


def _cadastro(rota: str) -> Cadastro:
    cadastro = CADASTROS.get(rota)
    if cadastro is None:
        raise HTTPException(HTTPStatus.NOT_FOUND)
    return cadastro


async def _tela(
    request: Request,
    cadastro: Cadastro,
    status: int = HTTPStatus.OK,
    registro: dict[str, Any] | None = None,
    erro: str | None = None,
) -> Response:
    busca = request.query_params.get("busca") if cadastro.filtro_de_busca else None
    registros = await chamar(request, "GET", cadastro.caminho_api, params={"busca": busca})
    return pagina(
        request,
        "cadastro.html",
        status,
        cadastro=cadastro,
        registros=[cadastro.exibir(r, request) for r in registros],
        registro=registro,
        opcoes=await opcoes_dos_campos(cadastro, request),
        avisos=cadastro.aviso(registros),
        erro=erro,
        busca=busca or "",
    )


def _corpo(cadastro: Cadastro, request: Request, formulario: Any, criando: bool) -> dict[str, Any]:
    valores = {chave: formulario.getlist(chave) for chave in formulario}
    return cadastro.ajustar(ler_formulario(cadastro, valores, criando), request)


async def _salvar(request: Request, cadastro: Cadastro, id_: str | None) -> Response:
    formulario = await request.form()
    registro = {chave: formulario.get(chave) for chave in formulario}
    try:
        corpo = _corpo(cadastro, request, formulario, id_ is None)
        if id_ is None:
            await chamar(request, "POST", cadastro.caminho_api, json=corpo)
        else:
            await chamar(request, "PUT", f"{cadastro.caminho_api}/{id_}", json=corpo)
    except ValueError:
        return await _tela(
            request, cadastro, HTTPStatus.UNPROCESSABLE_ENTITY, registro, "Confira os campos."
        )
    except ErroDaApi as erro:
        if erro.status == HTTPStatus.UNAUTHORIZED:
            raise
        return await _tela(request, cadastro, erro.status, {"id": id_, **registro}, erro.mensagem)
    return RedirectResponse(f"/cadastros/{cadastro.rota}", HTTPStatus.SEE_OTHER)


@router.get("/cadastros/{rota}")
async def listar(request: Request, rota: str) -> Response:
    return await _tela(request, _cadastro(rota))


@router.post("/cadastros/{rota}")
async def criar(request: Request, rota: str) -> Response:
    return await _salvar(request, _cadastro(rota), None)


@router.get("/cadastros/{rota}/{id_}")
async def editar(request: Request, rota: str, id_: str) -> Response:
    cadastro = _cadastro(rota)
    registros = await chamar(request, "GET", cadastro.caminho_api)
    registro = next((r for r in registros if r["id"] == id_), None)
    if registro is None:
        raise ErroDaApi(HTTPStatus.NOT_FOUND, "registro_nao_encontrado")
    return await _tela(request, cadastro, registro=cadastro.exibir(registro, request))


@router.post("/cadastros/{rota}/{id_}")
async def atualizar(request: Request, rota: str, id_: str) -> Response:
    return await _salvar(request, _cadastro(rota), id_)


@router.post("/cadastros/{rota}/{id_}/apagar")
async def apagar(request: Request, rota: str, id_: str) -> Response:
    cadastro = _cadastro(rota)
    if not cadastro.pode_apagar:
        raise HTTPException(HTTPStatus.METHOD_NOT_ALLOWED)
    try:
        await chamar(request, "DELETE", f"{cadastro.caminho_api}/{id_}")
    except ErroDaApi as erro:
        if erro.status == HTTPStatus.UNAUTHORIZED:
            raise
        return await _tela(request, cadastro, erro.status, erro=erro.mensagem)
    return RedirectResponse(f"/cadastros/{rota}", HTTPStatus.SEE_OTHER)
