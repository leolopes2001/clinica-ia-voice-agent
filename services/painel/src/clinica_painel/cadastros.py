"""Telas de cadastro, todas iguais: a tabela, o formulário e as chamadas à API."""

from datetime import datetime
from http import HTTPStatus
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from clinica_painel.api import ErroDaApi, chamar
from clinica_painel.formularios import Cadastro, Campo, ler_formulario, opcoes_dos_campos
from clinica_painel.telas import data_hora, no_fuso, pagina

router = APIRouter()

CADASTROS: dict[str, Cadastro] = {}
_DIAS = ("Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo")
_VIRGULAS = "Separadas por vírgula."


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
        le_um_por_vez=True,
        campos=[
            Campo("nome", "Nome completo"),
            Campo("telefone_e164", "Telefone", ajuda="Com DDD, como 11 98888-7777."),
            Campo("data_nascimento", "Data de nascimento", "date"),
        ],
    )
)


async def _dentistas(request: Request) -> list[tuple[str, str]]:
    dentistas = await chamar(request, "GET", "/painel/dentistas")
    return [(d["id"], d["nome"]) for d in dentistas]


async def _consultorios(request: Request) -> list[tuple[str, str]]:
    consultorios = await chamar(request, "GET", "/painel/consultorios")
    return [(c["id"], c["nome"]) for c in consultorios]


async def _dias_da_semana(_: Request) -> list[tuple[str, str]]:
    return [(str(i), nome) for i, nome in enumerate(_DIAS)]


def _exibir_grade(grade: dict[str, Any], _: Request) -> dict[str, Any]:
    return {
        **grade,
        "dia_semana": str(grade["dia_semana"]),
        "dia_semana_texto": _DIAS[grade["dia_semana"]],
        "dentista_id_texto": grade["dentista"],
        "consultorio_id_texto": grade["consultorio"],
        "inicio": grade["inicio"][:5],
        "fim": grade["fim"][:5],
    }


def _exibir_bloqueio(bloqueio: dict[str, Any], request: Request) -> dict[str, Any]:
    fuso = request.session["fuso"]
    return {
        **bloqueio,
        "dentista_id_texto": bloqueio["dentista"] or "Clínica toda",
        "inicio": no_fuso(bloqueio["inicio"], fuso).strftime("%Y-%m-%dT%H:%M"),
        "fim": no_fuso(bloqueio["fim"], fuso).strftime("%Y-%m-%dT%H:%M"),
        "inicio_texto": data_hora(bloqueio["inicio"], fuso),
        "fim_texto": data_hora(bloqueio["fim"], fuso),
    }


def _ajustar_bloqueio(corpo: dict[str, Any], request: Request) -> dict[str, Any]:
    # O campo do navegador não tem fuso: vale o da clínica.
    fuso = ZoneInfo(request.session["fuso"])
    return {
        **corpo,
        "inicio": datetime.fromisoformat(corpo["inicio"]).replace(tzinfo=fuso).isoformat(),
        "fim": datetime.fromisoformat(corpo["fim"]).replace(tzinfo=fuso).isoformat(),
    }


def _sem_dentista(procedimentos: list[dict[str, Any]]) -> list[str]:
    return [
        f"{p['nome']}: sem dentista habilitado. O agente não consegue oferecer horários."
        for p in procedimentos
        if not p["dentistas_habilitados"]
    ]


registrar(
    Cadastro(
        rota="dentistas",
        titulo="Dentistas",
        singular="dentista",
        caminho_api="/painel/dentistas",
        campos=[
            Campo("nome", "Nome"),
            Campo("especialidades", "Especialidades", "lista", obrigatorio=False, ajuda=_VIRGULAS),
            Campo("ativo", "Ativo", "checkbox", obrigatorio=False),
        ],
    )
)
registrar(
    Cadastro(
        rota="consultorios",
        titulo="Consultórios",
        singular="consultório",
        caminho_api="/painel/consultorios",
        campos=[
            Campo("nome", "Nome"),
            Campo("ativo", "Ativo", "checkbox", obrigatorio=False),
        ],
    )
)
registrar(
    Cadastro(
        rota="procedimentos",
        titulo="Procedimentos",
        singular="procedimento",
        caminho_api="/painel/procedimentos",
        aviso=_sem_dentista,
        campos=[
            Campo("nome", "Nome"),
            Campo(
                "sinonimos",
                "Sinônimos",
                "lista",
                obrigatorio=False,
                ajuda="Separados por vírgula. Ajudam o agente: limpeza, profilaxia.",
            ),
            Campo("duracao_min", "Duração (min)", "number"),
            Campo("intervalo_limpeza_min", "Intervalo de limpeza (min)", "number"),
            Campo(
                "dentistas_habilitados",
                "Dentistas habilitados",
                "multiselect",
                obrigatorio=False,
                opcoes=_dentistas,
                na_tabela=False,
            ),
        ],
    )
)
registrar(
    Cadastro(
        rota="grade",
        titulo="Grade de trabalho",
        singular="horário",
        caminho_api="/painel/grade",
        exibir=_exibir_grade,
        campos=[
            Campo("dentista_id", "Dentista", "select", opcoes=_dentistas),
            Campo("dia_semana", "Dia da semana", "select", opcoes=_dias_da_semana),
            Campo("inicio", "Início", "time"),
            Campo("fim", "Fim", "time"),
            Campo("consultorio_id", "Consultório", "select", opcoes=_consultorios),
        ],
    )
)
registrar(
    Cadastro(
        rota="bloqueios",
        titulo="Bloqueios",
        singular="bloqueio",
        caminho_api="/painel/bloqueios",
        exibir=_exibir_bloqueio,
        ajustar=_ajustar_bloqueio,
        campos=[
            Campo(
                "dentista_id",
                "Dentista",
                "select",
                obrigatorio=False,
                opcoes=_dentistas,
                ajuda="Clínica toda",
            ),
            Campo("inicio", "Início", "datetime-local"),
            Campo("fim", "Fim", "datetime-local"),
            Campo("motivo", "Motivo", ajuda="Férias, feriado, almoço."),
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


def _multiplos(cadastro: Cadastro) -> set[str]:
    return {c.nome for c in cadastro.campos if c.tipo == "multiselect"}


def _corpo(cadastro: Cadastro, request: Request, formulario: Any, criando: bool) -> dict[str, Any]:
    valores = {chave: formulario.getlist(chave) for chave in formulario}
    return cadastro.ajustar(ler_formulario(cadastro, valores, criando), request)


async def _salvar(request: Request, cadastro: Cadastro, id_: str | None) -> Response:
    formulario = await request.form()
    registro: dict[str, Any] = {
        chave: formulario.getlist(chave) if chave in _multiplos(cadastro) else formulario.get(chave)
        for chave in formulario
    }
    try:
        corpo = _corpo(cadastro, request, formulario, id_ is None)
        if id_ is None:
            await chamar(request, "POST", cadastro.caminho_api, json=corpo)
        else:
            await chamar(request, "PUT", f"{cadastro.caminho_api}/{id_}", json=corpo)
    except (TypeError, ValueError):
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
    if cadastro.le_um_por_vez:
        registro = await chamar(request, "GET", f"{cadastro.caminho_api}/{id_}")
    else:
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
