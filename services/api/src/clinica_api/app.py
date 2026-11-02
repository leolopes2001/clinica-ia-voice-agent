from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_api import agenda
from clinica_api.agenda import Contexto, ErroAgenda
from clinica_api.esquemas import (
    AgendamentoCancelado,
    AgendamentoConfirmado,
    CancelarEntrada,
    ConfirmarEntrada,
    Falha,
    HoldCriado,
    Horario,
    HorariosLivres,
    RemarcarEntrada,
    SegurarEntrada,
)
from clinica_api.horarios import ProcedimentoNaoEncontrado, listar_horarios_livres
from clinica_api.publicacao import PublicadorHolds, PublicadorRabbit
from clinica_core.config import ConfigAgenda, ConfigPostgres, ConfigRabbit
from clinica_core.db import criar_engine, criar_fabrica_sessoes

_STATUS_POR_MOTIVO = {
    "contexto_ausente": HTTPStatus.BAD_REQUEST,
    "contexto_divergente": HTTPStatus.FORBIDDEN,
    "identidade_nao_verificada": HTTPStatus.FORBIDDEN,
    "agendamento_nao_encontrado": HTTPStatus.NOT_FOUND,
    "chamada_nao_encontrada": HTTPStatus.NOT_FOUND,
    "clinica_nao_encontrada": HTTPStatus.NOT_FOUND,
    "paciente_nao_encontrado": HTTPStatus.NOT_FOUND,
    "paciente_nao_permitido": HTTPStatus.UNPROCESSABLE_ENTITY,
    "paciente_obrigatorio": HTTPStatus.UNPROCESSABLE_ENTITY,
}


async def _sessao(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.fabrica_sessoes() as sessao:
        yield sessao


Sessao = Annotated[AsyncSession, Depends(_sessao)]


async def _contexto(
    sessao: Sessao,
    x_chamada_id: Annotated[UUID | None, Header()] = None,
    x_clinica_id: Annotated[UUID | None, Header()] = None,
) -> Contexto:
    return await agenda.resolver_contexto(sessao, x_chamada_id, x_clinica_id)


def _config(request: Request) -> ConfigAgenda:
    config: ConfigAgenda = request.app.state.config_agenda
    return config


ContextoDaRequisicao = Annotated[Contexto, Depends(_contexto)]
Agenda = Annotated[ConfigAgenda, Depends(_config)]
ChaveIdempotencia = Annotated[str, Header(alias="Idempotency-Key", min_length=1)]


def _falha(request: Request, erro: Exception) -> JSONResponse:
    assert isinstance(erro, ErroAgenda)
    corpo = Falha(motivo=erro.motivo, alternativas=erro.alternativas)
    # 409 para o que o agente resolve na conversa: ocupado, indisponível, hold perdido...
    status = _STATUS_POR_MOTIVO.get(erro.motivo, HTTPStatus.CONFLICT)
    return JSONResponse(corpo.model_dump(mode="json"), status_code=status)


def _procedimento_nao_encontrado(request: Request, erro: Exception) -> JSONResponse:
    corpo = Falha(motivo="procedimento_nao_encontrado")
    return JSONResponse(corpo.model_dump(mode="json"), status_code=HTTPStatus.NOT_FOUND)


def criar_app(
    config_postgres: ConfigPostgres | None = None,
    config_agenda: ConfigAgenda | None = None,
    publicador: PublicadorHolds | None = None,
) -> FastAPI:
    engine = criar_engine(config_postgres or ConfigPostgres())
    publicador_holds = publicador or PublicadorRabbit(ConfigRabbit())

    @asynccontextmanager
    async def ciclo_de_vida(_: FastAPI) -> AsyncIterator[None]:
        await publicador_holds.abrir()
        yield
        await publicador_holds.fechar()
        await engine.dispose()

    app = FastAPI(title="Clínica Voz: API", lifespan=ciclo_de_vida)
    app.state.engine = engine
    app.state.fabrica_sessoes = criar_fabrica_sessoes(engine)
    app.state.config_agenda = config_agenda or ConfigAgenda()
    app.add_exception_handler(ErroAgenda, _falha)
    app.add_exception_handler(ProcedimentoNaoEncontrado, _procedimento_nao_encontrado)

    @app.get("/saude")
    async def saude(sessao: Sessao) -> dict[str, str]:
        await sessao.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/horarios-livres")
    async def horarios_livres(
        sessao: Sessao,
        contexto: ContextoDaRequisicao,
        config: Agenda,
        procedimento_id: UUID,
        a_partir_de: AwareDatetime,
        ate: AwareDatetime,
        dentista_id: Annotated[UUID | None, Query()] = None,
    ) -> HorariosLivres:
        horarios = await listar_horarios_livres(
            sessao, contexto.clinica_id, procedimento_id, a_partir_de, ate, config, dentista_id
        )
        return HorariosLivres(horarios=[Horario.model_validate(h) for h in horarios])

    @app.post("/holds", status_code=HTTPStatus.CREATED)
    async def segurar(
        entrada: SegurarEntrada,
        sessao: Sessao,
        contexto: ContextoDaRequisicao,
        config: Agenda,
        chave: ChaveIdempotencia,
    ) -> HoldCriado:
        return await agenda.segurar(sessao, contexto, entrada, chave, config, publicador_holds)

    @app.post("/holds/{hold_id}/confirmar")
    async def confirmar(
        hold_id: UUID,
        entrada: ConfirmarEntrada,
        sessao: Sessao,
        contexto: ContextoDaRequisicao,
        chave: ChaveIdempotencia,
    ) -> AgendamentoConfirmado:
        return await agenda.confirmar(sessao, contexto, hold_id, entrada, chave)

    @app.post("/agendamentos/{agendamento_id}/remarcar")
    async def remarcar(
        agendamento_id: UUID,
        entrada: RemarcarEntrada,
        sessao: Sessao,
        contexto: ContextoDaRequisicao,
        chave: ChaveIdempotencia,
    ) -> AgendamentoConfirmado:
        return await agenda.remarcar(sessao, contexto, agendamento_id, entrada, chave)

    @app.post("/agendamentos/{agendamento_id}/cancelar")
    async def cancelar(
        agendamento_id: UUID,
        entrada: CancelarEntrada,
        sessao: Sessao,
        contexto: ContextoDaRequisicao,
        chave: ChaveIdempotencia,
    ) -> AgendamentoCancelado:
        return await agenda.cancelar(sessao, contexto, agendamento_id, entrada, chave)

    return app
