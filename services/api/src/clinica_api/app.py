from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime
from sqlalchemy import text

from clinica_api import (
    agenda,
    chamadas,
    configuracao_agente,
    painel,
    painel_cadastros,
    painel_operacao,
    voz,
)
from clinica_api.agenda import Contexto, ErroAgenda
from clinica_api.autenticacao import Sessao, exigir_token, papel_do_token
from clinica_api.esquemas import (
    AgendamentoCancelado,
    AgendamentoConfirmado,
    CadastrarPacienteEntrada,
    CancelamentoPreparado,
    CancelarEntrada,
    ChamadaCriada,
    Clinicas,
    ConfirmarEntrada,
    ConsentimentoEntrada,
    ConsentimentoRegistrado,
    ConsultasFuturas,
    CriarChamadaEntrada,
    Falha,
    HoldCriado,
    HoldFalado,
    Horario,
    HorariosLivres,
    OpcoesHorario,
    PacienteDaChamada,
    PacientesDoTelefone,
    PedidoRetornoCriado,
    PedidoRetornoEntrada,
    Procedimentos,
    RemarcacaoPreparada,
    RemarcarEntrada,
    ResultadoVerificacao,
    SegurarEntrada,
    SegurarPorNomeEntrada,
    SelecionarPacienteEntrada,
    Turno,
    VerificarIdentidadeEntrada,
)
from clinica_api.horarios import ProcedimentoNaoEncontrado, listar_horarios_livres
from clinica_api.publicacao import PublicadorAgenda, PublicadorRabbit
from clinica_core.config import (
    ConfigAgenda,
    ConfigPostgres,
    ConfigRabbit,
    ConfigRetencao,
    ConfigTokens,
)
from clinica_core.db import criar_engine, criar_fabrica_sessoes
from clinica_core.logs import configurar_logs

_STATUS_POR_MOTIVO = {
    "contexto_ausente": HTTPStatus.BAD_REQUEST,
    "nao_autenticado": HTTPStatus.UNAUTHORIZED,
    "credenciais_invalidas": HTTPStatus.UNAUTHORIZED,
    "login_bloqueado": HTTPStatus.TOO_MANY_REQUESTS,
    "token_nao_permitido": HTTPStatus.FORBIDDEN,
    "sem_permissao": HTTPStatus.FORBIDDEN,
    "registro_nao_encontrado": HTTPStatus.NOT_FOUND,
    "telefone_invalido": HTTPStatus.UNPROCESSABLE_ENTITY,
    "numero_transferencia_invalido": HTTPStatus.UNPROCESSABLE_ENTITY,
    "exige_chamada": HTTPStatus.BAD_REQUEST,
    "exige_painel": HTTPStatus.BAD_REQUEST,
    "dentista_nao_encontrado": HTTPStatus.NOT_FOUND,
    "procedimento_nao_encontrado": HTTPStatus.NOT_FOUND,
    "contexto_divergente": HTTPStatus.FORBIDDEN,
    "identidade_nao_verificada": HTTPStatus.FORBIDDEN,
    "consentimento_ausente": HTTPStatus.FORBIDDEN,
    "agendamento_nao_encontrado": HTTPStatus.NOT_FOUND,
    "chamada_nao_encontrada": HTTPStatus.NOT_FOUND,
    "clinica_nao_encontrada": HTTPStatus.NOT_FOUND,
    "paciente_nao_encontrado": HTTPStatus.NOT_FOUND,
    "paciente_nao_permitido": HTTPStatus.UNPROCESSABLE_ENTITY,
    "paciente_obrigatorio": HTTPStatus.UNPROCESSABLE_ENTITY,
}


async def _contexto(
    sessao: Sessao,
    papel: Annotated[str, Depends(papel_do_token)],
    x_chamada_id: Annotated[UUID | None, Header()] = None,
    x_clinica_id: Annotated[UUID | None, Header()] = None,
    x_usuario_id: Annotated[UUID | None, Header()] = None,
) -> Contexto:
    # A voz age dentro de uma chamada; só o painel age em nome da clínica.
    exigir_token(papel, "servicos" if x_chamada_id is not None else "painel")
    return await agenda.resolver_contexto(sessao, x_chamada_id, x_clinica_id, x_usuario_id)


async def _contexto_de_abertura(
    sessao: Sessao,
    _: Annotated[str, Depends(papel_do_token)],
    x_chamada_id: Annotated[UUID | None, Header()] = None,
    x_clinica_id: Annotated[UUID | None, Header()] = None,
) -> Contexto:
    # O agente abre a chamada antes de ela existir, com o token dele.
    return await agenda.resolver_contexto(sessao, x_chamada_id, x_clinica_id)


async def _config(
    request: Request, sessao: Sessao, contexto: Annotated[Contexto, Depends(_contexto)]
) -> ConfigAgenda:
    configurada = await configuracao_agente.configuracao_da_agenda(
        sessao, contexto.clinica_id, request.app.state.config_agenda
    )
    # Encerra a leitura: o now() de cada operação não deve vir de antes dela.
    await sessao.commit()
    return configurada


ContextoDaRequisicao = Annotated[Contexto, Depends(_contexto)]
ContextoDeAbertura = Annotated[Contexto, Depends(_contexto_de_abertura)]


def _contexto_com_consentimento(contexto: ContextoDaRequisicao) -> Contexto:
    agenda.exigir_consentimento(contexto)
    return contexto


ContextoComConsentimento = Annotated[Contexto, Depends(_contexto_com_consentimento)]
Agenda = Annotated[ConfigAgenda, Depends(_config)]
ChaveIdempotencia = Annotated[str, Header(alias="Idempotency-Key", min_length=1)]


def _falha(request: Request, erro: Exception) -> JSONResponse:
    assert isinstance(erro, ErroAgenda)
    corpo = Falha(motivo=erro.motivo, alternativas=erro.alternativas, opcoes=erro.opcoes)
    # 409 para o que o agente resolve na conversa: ocupado, indisponível, hold perdido...
    status = _STATUS_POR_MOTIVO.get(erro.motivo, HTTPStatus.CONFLICT)
    return JSONResponse(corpo.model_dump(mode="json"), status_code=status)


def _procedimento_nao_encontrado(request: Request, erro: Exception) -> JSONResponse:
    corpo = Falha(motivo="procedimento_nao_encontrado")
    return JSONResponse(corpo.model_dump(mode="json"), status_code=HTTPStatus.NOT_FOUND)


def criar_app(
    config_postgres: ConfigPostgres | None = None,
    config_agenda: ConfigAgenda | None = None,
    publicador: PublicadorAgenda | None = None,
    config_tokens: ConfigTokens | None = None,
) -> FastAPI:
    engine = criar_engine(config_postgres or ConfigPostgres())
    publicador_agenda = publicador or PublicadorRabbit(ConfigRabbit())
    config_retencao = ConfigRetencao()

    @asynccontextmanager
    async def ciclo_de_vida(_: FastAPI) -> AsyncIterator[None]:
        await publicador_agenda.abrir()
        yield
        await publicador_agenda.fechar()
        await engine.dispose()

    app = FastAPI(title="Clínica Voz: API", lifespan=ciclo_de_vida)
    app.state.engine = engine
    app.state.fabrica_sessoes = criar_fabrica_sessoes(engine)
    app.state.config_agenda = config_agenda or ConfigAgenda()
    app.state.config_tokens = config_tokens or ConfigTokens()
    app.state.publicador = publicador_agenda
    app.include_router(painel.router)
    app.include_router(painel_cadastros.router)
    app.include_router(painel_operacao.router)
    app.include_router(configuracao_agente.router)
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
        contexto: ContextoComConsentimento,
        config: Agenda,
        chave: ChaveIdempotencia,
    ) -> HoldCriado:
        return await agenda.segurar(sessao, contexto, entrada, chave, config, publicador_agenda)

    @app.post("/holds/{hold_id}/confirmar")
    async def confirmar(
        hold_id: UUID,
        entrada: ConfirmarEntrada,
        sessao: Sessao,
        contexto: ContextoComConsentimento,
        chave: ChaveIdempotencia,
    ) -> AgendamentoConfirmado:
        return await agenda.confirmar(sessao, contexto, hold_id, entrada, chave, publicador_agenda)

    @app.post("/agendamentos/{agendamento_id}/remarcar/preparar")
    async def preparar_remarcacao(
        agendamento_id: UUID,
        entrada: RemarcarEntrada,
        sessao: Sessao,
        contexto: ContextoComConsentimento,
    ) -> RemarcacaoPreparada:
        return await voz.preparar_remarcacao(sessao, contexto, agendamento_id, entrada)

    @app.post("/agendamentos/{agendamento_id}/cancelar/preparar")
    async def preparar_cancelamento(
        agendamento_id: UUID, sessao: Sessao, contexto: ContextoComConsentimento
    ) -> CancelamentoPreparado:
        return await voz.preparar_cancelamento(sessao, contexto, agendamento_id)

    @app.post("/agendamentos/{agendamento_id}/remarcar")
    async def remarcar(
        agendamento_id: UUID,
        entrada: RemarcarEntrada,
        sessao: Sessao,
        contexto: ContextoComConsentimento,
        chave: ChaveIdempotencia,
    ) -> AgendamentoConfirmado:
        return await agenda.remarcar(
            sessao, contexto, agendamento_id, entrada, chave, publicador_agenda
        )

    @app.post("/agendamentos/{agendamento_id}/cancelar")
    async def cancelar(
        agendamento_id: UUID,
        entrada: CancelarEntrada,
        sessao: Sessao,
        contexto: ContextoComConsentimento,
        chave: ChaveIdempotencia,
    ) -> AgendamentoCancelado:
        return await agenda.cancelar(
            sessao, contexto, agendamento_id, entrada, chave, publicador_agenda
        )

    @app.get("/clinicas", dependencies=[Depends(papel_do_token)])
    async def clinicas(sessao: Sessao) -> Clinicas:
        return await chamadas.listar_clinicas(sessao)

    @app.post("/chamadas", status_code=HTTPStatus.CREATED)
    async def criar_chamada(
        entrada: CriarChamadaEntrada,
        sessao: Sessao,
        contexto: ContextoDeAbertura,
        chave: ChaveIdempotencia,
    ) -> ChamadaCriada:
        return await chamadas.criar_chamada(sessao, contexto, entrada, chave)

    @app.post("/chamadas/consentimento")
    async def consentimento(
        entrada: ConsentimentoEntrada, sessao: Sessao, contexto: ContextoDaRequisicao
    ) -> ConsentimentoRegistrado:
        return await chamadas.registrar_consentimento(sessao, contexto, entrada)

    @app.get("/chamadas/configuracao")
    async def configuracao_da_ligacao(
        sessao: Sessao, contexto: ContextoDaRequisicao
    ) -> configuracao_agente.ConfiguracaoDaLigacao:
        return await chamadas.configuracao_da_ligacao(sessao, contexto, config_retencao)

    @app.post("/identidade/identificar")
    async def identificar(
        sessao: Sessao, contexto: ContextoComConsentimento
    ) -> PacientesDoTelefone:
        return await chamadas.identificar(sessao, contexto)

    @app.post("/identidade/selecionar")
    async def selecionar(
        entrada: SelecionarPacienteEntrada, sessao: Sessao, contexto: ContextoComConsentimento
    ) -> PacienteDaChamada:
        return await chamadas.selecionar(sessao, contexto, entrada)

    @app.post("/identidade/cadastrar")
    async def cadastrar(
        entrada: CadastrarPacienteEntrada, sessao: Sessao, contexto: ContextoComConsentimento
    ) -> PacienteDaChamada:
        return await chamadas.cadastrar(sessao, contexto, entrada)

    @app.post("/identidade/verificar")
    async def verificar(
        entrada: VerificarIdentidadeEntrada, sessao: Sessao, contexto: ContextoComConsentimento
    ) -> ResultadoVerificacao:
        return await chamadas.verificar(sessao, contexto, entrada)

    @app.post("/pedidos-retorno", status_code=HTTPStatus.CREATED)
    async def pedido_retorno(
        entrada: PedidoRetornoEntrada,
        sessao: Sessao,
        contexto: ContextoComConsentimento,
        chave: ChaveIdempotencia,
    ) -> PedidoRetornoCriado:
        return await chamadas.registrar_pedido_retorno(sessao, contexto, entrada, chave)

    @app.get("/procedimentos")
    async def procedimentos(sessao: Sessao, contexto: ContextoDaRequisicao) -> Procedimentos:
        return await voz.listar_procedimentos(sessao, contexto)

    @app.get("/opcoes-horarios")
    async def opcoes_horarios(
        sessao: Sessao,
        contexto: ContextoDaRequisicao,
        config: Agenda,
        procedimento: str,
        a_partir_de: AwareDatetime | date,
        ate: AwareDatetime | date,
        dentista: Annotated[str | None, Query()] = None,
        turno: Annotated[Turno | None, Query()] = None,
    ) -> OpcoesHorario:
        return await voz.opcoes_horarios(
            sessao, contexto, config, procedimento, a_partir_de, ate, dentista, turno
        )

    @app.post("/holds/por-nome", status_code=HTTPStatus.CREATED)
    async def segurar_por_nome(
        entrada: SegurarPorNomeEntrada,
        sessao: Sessao,
        contexto: ContextoComConsentimento,
        config: Agenda,
        chave: ChaveIdempotencia,
    ) -> HoldFalado:
        return await voz.segurar_por_nome(
            sessao, contexto, entrada, chave, config, publicador_agenda
        )

    @app.get("/agendamentos/meus")
    async def meus_agendamentos(
        sessao: Sessao, contexto: ContextoComConsentimento
    ) -> ConsultasFuturas:
        return await voz.listar_meus_agendamentos(sessao, contexto)

    return app


def criar_app_de_producao() -> FastAPI:
    configurar_logs("api")
    return criar_app()
