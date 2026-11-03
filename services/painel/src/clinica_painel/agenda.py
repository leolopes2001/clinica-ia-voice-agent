"""Agenda: a tela relê tudo da API a cada aviso do SSE, sem depender da ordem dos eventos."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import date, datetime, time, timedelta
from http import HTTPStatus
from itertools import groupby
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse

from clinica_painel.api import ErroDaApi, chamar
from clinica_painel.eventos import FonteDeEventos, assinatura
from clinica_painel.telas import no_fuso, pagina

router = APIRouter()

_DIAS = ("Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo")
_BATIMENTO_S = 15.0


def _hoje(request: Request) -> date:
    return datetime.now(ZoneInfo(request.session["fuso"])).date()


def _dia(request: Request, valor: str | None) -> date:
    try:
        return date.fromisoformat(valor) if valor else _hoje(request)
    except ValueError:
        return _hoje(request)


def _periodo(dia: date, visao: str, fuso: ZoneInfo) -> tuple[date, datetime, datetime]:
    primeiro = dia - timedelta(days=dia.weekday()) if visao == "semana" else dia
    dias = 7 if visao == "semana" else 1
    inicio = datetime.combine(primeiro, time.min, tzinfo=fuso)
    fim = datetime.combine(primeiro + timedelta(days=dias), time.min, tzinfo=fuso)
    return primeiro, inicio, fim


def _titulo_do_dia(dia: date) -> str:
    return f"{_DIAS[dia.weekday()]}, {dia:%d/%m}"


@router.get("/agenda")
async def ver_agenda(
    request: Request, dia: str | None = None, visao: str = "dia", dentista: str = ""
) -> Response:
    escolhido = _dia(request, dia)
    visao = "semana" if visao == "semana" else "dia"
    passo = timedelta(days=7 if visao == "semana" else 1)
    return pagina(
        request,
        "agenda.html",
        dia=escolhido,
        visao=visao,
        dentista=dentista,
        dentistas=await chamar(request, "GET", "/painel/dentistas"),
        anterior=escolhido - passo,
        seguinte=escolhido + passo,
        hoje=_hoje(request),
    )


@router.get("/agenda/grade")
async def ver_grade(
    request: Request, dia: str | None = None, visao: str = "dia", dentista: str = ""
) -> Response:
    fuso = ZoneInfo(request.session["fuso"])
    visao = "semana" if visao == "semana" else "dia"
    primeiro, inicio, fim = _periodo(_dia(request, dia), visao, fuso)
    consultas = await chamar(
        request,
        "GET",
        "/painel/agenda",
        params={
            "inicio": inicio.isoformat(),
            "fim": fim.isoformat(),
            "dentista_id": dentista,
        },
    )

    def dia_da_consulta(consulta: dict[str, Any]) -> date:
        return no_fuso(consulta["inicio"], request.session["fuso"]).date()

    por_dia = {d: list(c) for d, c in groupby(consultas, key=dia_da_consulta)}
    dias = [
        (_titulo_do_dia(d), por_dia.get(d, []))
        for d in (primeiro + timedelta(days=i) for i in range(7 if visao == "semana" else 1))
    ]
    return pagina(request, "_grade.html", dias=dias)


async def gerar_avisos(
    fonte: FonteDeEventos,
    clinica_id: str,
    desconectou: Callable[[], Awaitable[bool]],
    batimento_s: float = _BATIMENTO_S,
) -> AsyncGenerator[str]:
    """Só o tipo do evento: quem desenha a agenda é a página, sob o login dela."""
    async with assinatura(fonte, clinica_id) as fila:
        while not await desconectou():
            try:
                async with asyncio.timeout(batimento_s):
                    tipo = await fila.get()
            except TimeoutError:
                yield ": vivo\n\n"
            else:
                yield f"data: {tipo}\n\n"


@router.get("/agenda/eventos")
async def eventos(request: Request) -> StreamingResponse:
    fonte: FonteDeEventos = request.app.state.eventos
    return StreamingResponse(
        gerar_avisos(fonte, request.session["clinica_id"], request.is_disconnected),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


async def _listas_do_formulario(request: Request) -> dict[str, Any]:
    dentistas = await chamar(request, "GET", "/painel/dentistas")
    return {
        "dentistas": [d for d in dentistas if d["ativo"]],
        "procedimentos": await chamar(request, "GET", "/painel/procedimentos"),
    }


async def _tela_nova(
    request: Request, status: int = HTTPStatus.OK, erro: str | None = None, **valores: Any
) -> Response:
    return pagina(
        request,
        "agenda_nova.html",
        status,
        erro=erro,
        chave=uuid4().hex,
        hoje=_hoje(request),
        valores=valores,
        **await _listas_do_formulario(request),
    )


@router.get("/agenda/nova")
async def ver_nova(request: Request) -> Response:
    return await _tela_nova(request)


@router.get("/agenda/horarios")
async def ver_horarios(
    request: Request, procedimento_id: str = "", dentista_id: str = "", data: str = ""
) -> Response:
    """Os horários livres de um dia. Parte da tela: quem escolhe é a recepção."""
    if not (procedimento_id and dentista_id and data):
        return pagina(request, "_horarios.html", horarios=[], incompleto=True)
    fuso = ZoneInfo(request.session["fuso"])
    dia = _dia(request, data)
    resposta = await chamar(
        request,
        "GET",
        "/horarios-livres",
        params={
            "procedimento_id": procedimento_id,
            "dentista_id": dentista_id,
            "a_partir_de": datetime.combine(dia, time.min, tzinfo=fuso).isoformat(),
            "ate": datetime.combine(dia, time.max, tzinfo=fuso).isoformat(),
        },
    )
    return pagina(request, "_horarios.html", horarios=resposta["horarios"], incompleto=False)


@router.get("/agenda/pacientes")
async def buscar_pacientes(request: Request, busca: str = "") -> Response:
    pacientes = await chamar(request, "GET", "/painel/pacientes", params={"busca": busca})
    return pagina(request, "_pacientes.html", pacientes=pacientes)


async def _segurar(
    request: Request, procedimento_id: str, dentista_id: str, inicio: str, chave: str
) -> str:
    hold = await chamar(
        request,
        "POST",
        "/holds",
        json={"procedimento_id": procedimento_id, "dentista_id": dentista_id, "inicio": inicio},
        chave=f"{chave}:segurar",
    )
    return str(hold["hold_id"])


@router.post("/agenda/nova")
async def criar_consulta(
    request: Request,
    chave: str = Form(),
    procedimento_id: str = Form(),
    dentista_id: str = Form(),
    data: str = Form(),
    inicio: str = Form(""),
    paciente_id: str = Form(""),
) -> Response:
    valores = {"procedimento_id": procedimento_id, "dentista_id": dentista_id, "data": data}
    if not inicio or not paciente_id:
        return await _tela_nova(
            request,
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Escolha o horário e o paciente.",
            **valores,
        )
    try:
        hold_id = await _segurar(request, procedimento_id, dentista_id, inicio, chave)
        await chamar(
            request,
            "POST",
            f"/holds/{hold_id}/confirmar",
            json={"paciente_id": paciente_id},
            chave=f"{chave}:confirmar",
        )
    except ErroDaApi as erro:
        if erro.status == HTTPStatus.UNAUTHORIZED:
            raise
        # A lista de horários é relida, e a recepção vê as alternativas na mesma tela.
        return await _tela_nova(request, erro.status, erro.mensagem, **valores)
    return RedirectResponse(f"/agenda?dia={data}", HTTPStatus.SEE_OTHER)


async def _tela_da_consulta(
    request: Request, consulta_id: str, status: int = HTTPStatus.OK, erro: str | None = None
) -> Response:
    consulta = await chamar(request, "GET", f"/painel/agendamentos/{consulta_id}")
    return pagina(
        request,
        "agenda_consulta.html",
        status,
        consulta=consulta,
        erro=erro,
        chave=uuid4().hex,
        hoje=_hoje(request),
        **await _listas_do_formulario(request),
    )


@router.get("/agenda/consulta/{consulta_id}")
async def ver_consulta(request: Request, consulta_id: str) -> Response:
    return await _tela_da_consulta(request, consulta_id)


@router.post("/agenda/consulta/{consulta_id}/remarcar")
async def remarcar(
    request: Request,
    consulta_id: str,
    chave: str = Form(),
    procedimento_id: str = Form(),
    dentista_id: str = Form(),
    inicio: str = Form(""),
) -> Response:
    if not inicio:
        return await _tela_da_consulta(
            request, consulta_id, HTTPStatus.UNPROCESSABLE_ENTITY, "Escolha o novo horário."
        )
    try:
        # O antigo só é liberado junto com a confirmação do novo, na mesma transação da API.
        hold_id = await _segurar(request, procedimento_id, dentista_id, inicio, chave)
        await chamar(
            request,
            "POST",
            f"/agendamentos/{consulta_id}/remarcar",
            json={"hold_id": hold_id},
            chave=f"{chave}:remarcar",
        )
    except ErroDaApi as erro:
        if erro.status == HTTPStatus.UNAUTHORIZED:
            raise
        return await _tela_da_consulta(request, consulta_id, erro.status, erro.mensagem)
    return RedirectResponse("/agenda", HTTPStatus.SEE_OTHER)


@router.post("/agenda/consulta/{consulta_id}/cancelar")
async def cancelar(
    request: Request, consulta_id: str, chave: str = Form(), motivo: str = Form("")
) -> Response:
    try:
        await chamar(
            request,
            "POST",
            f"/agendamentos/{consulta_id}/cancelar",
            json={"motivo": motivo.strip() or None},
            chave=chave,
        )
    except ErroDaApi as erro:
        if erro.status == HTTPStatus.UNAUTHORIZED:
            raise
        return await _tela_da_consulta(request, consulta_id, erro.status, erro.mensagem)
    return RedirectResponse("/agenda", HTTPStatus.SEE_OTHER)


@router.post("/agenda/consulta/{consulta_id}/status")
async def mudar_status(request: Request, consulta_id: str, status: str = Form()) -> Response:
    try:
        await chamar(
            request,
            "POST",
            f"/painel/agendamentos/{consulta_id}/status",
            json={"status": status},
        )
    except ErroDaApi as erro:
        if erro.status == HTTPStatus.UNAUTHORIZED:
            raise
        return await _tela_da_consulta(request, consulta_id, erro.status, erro.mensagem)
    return RedirectResponse("/agenda", HTTPStatus.SEE_OTHER)
