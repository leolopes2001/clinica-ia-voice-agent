from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from clinica_painel.api import ErroDaApi, chamar
from clinica_painel.telas import pagina

router = APIRouter()

_DIAS = ("Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo")


def _corpo(formulario: Any) -> dict[str, Any]:
    horario: dict[str, dict[str, str]] = {}
    for dia in range(len(_DIAS)):
        inicio, fim = formulario.get(f"inicio_{dia}", ""), formulario.get(f"fim_{dia}", "")
        if inicio or fim:
            # Só um dos dois preenchido cai na validação da API, em vez de sumir sem aviso.
            horario[str(dia)] = {"inicio": inicio, "fim": fim}
    antecedencia = (formulario.get("antecedencia_minima_min") or "").strip()
    return {
        "saudacao": (formulario.get("saudacao") or "").strip() or None,
        "voz": formulario.get("voz") or None,
        "horario_comercial": horario or None,
        "numero_transferencia": (formulario.get("numero_transferencia") or "").strip() or None,
        "antecedencia_minima_min": int(antecedencia) if antecedencia else None,
    }


async def _tela(
    request: Request, status: int = HTTPStatus.OK, erro: str | None = None, aviso: str | None = None
) -> Response:
    configuracao = await chamar(request, "GET", "/painel/configuracoes")
    return pagina(
        request,
        "configuracoes.html",
        status,
        c=configuracao,
        dias=list(enumerate(_DIAS)),
        erro=erro,
        aviso=aviso,
    )


@router.get("/configuracoes")
async def ver(request: Request) -> Response:
    return await _tela(request, aviso="Salvo." if request.query_params.get("salvo") else None)


@router.post("/configuracoes")
async def salvar(request: Request) -> Response:
    formulario = await request.form()
    try:
        corpo = _corpo(formulario)
    except ValueError:
        return await _tela(request, HTTPStatus.UNPROCESSABLE_ENTITY, "Confira os campos.")
    try:
        await chamar(request, "PUT", "/painel/configuracoes", json=corpo)
    except ErroDaApi as erro:
        if erro.status == HTTPStatus.UNAUTHORIZED:
            raise
        return await _tela(request, erro.status, erro.mensagem)
    return RedirectResponse("/configuracoes?salvo=1", HTTPStatus.SEE_OTHER)
