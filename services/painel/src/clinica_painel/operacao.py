"""Telas de acompanhamento: chamadas e pedidos de retorno."""

from http import HTTPStatus

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from clinica_painel.api import chamar
from clinica_painel.telas import pagina

router = APIRouter()

DESFECHOS = {
    "agendou": "Agendou",
    "remarcou": "Remarcou",
    "cancelou": "Cancelou",
    "duvida": "Dúvida",
    "transferiu": "Transferiu",
    "abandonou": "Abandonou",
}


@router.get("/chamadas")
async def ver_chamadas(request: Request, desfecho: str = "") -> Response:
    chamadas = await chamar(request, "GET", "/painel/chamadas", params={"desfecho": desfecho})
    return pagina(
        request,
        "chamadas.html",
        chamadas=chamadas,
        desfechos=DESFECHOS,
        desfecho=desfecho,
    )


@router.get("/chamadas/{id_}")
async def ver_chamada(request: Request, id_: str) -> Response:
    chamada = await chamar(request, "GET", f"/painel/chamadas/{id_}")
    return pagina(request, "chamada.html", chamada=chamada, desfechos=DESFECHOS)


@router.get("/retornos")
async def ver_retornos(request: Request) -> Response:
    return pagina(
        request, "retornos.html", retornos=await chamar(request, "GET", "/painel/retornos")
    )


@router.post("/retornos/{id_}/concluir")
async def concluir_retorno(request: Request, id_: str) -> Response:
    await chamar(request, "POST", f"/painel/retornos/{id_}/concluir")
    return RedirectResponse("/retornos", HTTPStatus.SEE_OTHER)
