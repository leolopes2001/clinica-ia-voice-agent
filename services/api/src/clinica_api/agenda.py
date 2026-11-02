import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_api import idempotencia
from clinica_api.esquemas import (
    AgendamentoCancelado,
    AgendamentoConfirmado,
    CancelarEntrada,
    ConfirmarEntrada,
    HoldCriado,
    Horario,
    RemarcarEntrada,
    SegurarEntrada,
)
from clinica_api.horarios import (
    HorarioLivre,
    listar_alternativas,
    localizar_horario_na_grade,
)
from clinica_api.publicacao import PublicadorAgenda
from clinica_api.travas import travar
from clinica_core.config import ConfigAgenda
from clinica_core.eventos import EventoAgendamento, criar_evento
from clinica_core.modelos import Agendamento, Chamada, Clinica, Paciente

_SQLSTATE_EXCLUSAO = "23P01"


class ErroAgenda(Exception):
    def __init__(self, motivo: str, alternativas: Sequence[HorarioLivre] = ()) -> None:
        super().__init__(motivo)
        self.motivo = motivo
        self.alternativas = [Horario.model_validate(h) for h in alternativas]


@dataclass(frozen=True)
class Contexto:
    clinica_id: UUID
    origem: str
    chamada_id: UUID | None = None
    paciente_id: UUID | None = None
    identidade_verificada: bool = False

    @property
    def voz(self) -> bool:
        return self.origem == "voz"


async def resolver_contexto(
    sessao: AsyncSession, chamada_id: UUID | None, clinica_id: UUID | None
) -> Contexto:
    contexto = await _ler_contexto(sessao, chamada_id, clinica_id)
    # Encerra a leitura: o now() de cada operação não deve vir de antes dela.
    await sessao.commit()
    return contexto


async def _ler_contexto(
    sessao: AsyncSession, chamada_id: UUID | None, clinica_id: UUID | None
) -> Contexto:
    if chamada_id is not None:
        chamada = await sessao.get(Chamada, chamada_id)
        if chamada is None:
            raise ErroAgenda("chamada_nao_encontrada")
        if clinica_id is not None and clinica_id != chamada.clinica_id:
            raise ErroAgenda("contexto_divergente")
        return Contexto(
            chamada.clinica_id,
            "voz",
            chamada.id,
            chamada.paciente_id,
            chamada.paciente_id is not None and chamada.identidade_verificada_em is not None,
        )
    if clinica_id is None:
        raise ErroAgenda("contexto_ausente")
    if await sessao.get(Clinica, clinica_id) is None:
        raise ErroAgenda("clinica_nao_encontrada")
    return Contexto(clinica_id, "painel")


@asynccontextmanager
async def _transacao(sessao: AsyncSession) -> AsyncIterator[None]:
    try:
        yield
        await sessao.commit()
    except Exception:
        await sessao.rollback()
        raise


def _chave(contexto: Contexto, operacao: str, chave: str) -> str:
    return f"{contexto.clinica_id}:{operacao}:{chave}"


async def _agora(sessao: AsyncSession) -> datetime:
    return (await sessao.execute(select(func.now()))).scalar_one()


def _inicio(agendamento: Agendamento) -> datetime:
    # O limite inferior sempre existe: a API só grava [inicio, fim).
    assert agendamento.periodo.lower is not None
    return agendamento.periodo.lower


def _fim_periodo(agendamento: Agendamento) -> datetime:
    assert agendamento.periodo.upper is not None
    return agendamento.periodo.upper


def _confirmado(agendamento: Agendamento) -> AgendamentoConfirmado:
    assert agendamento.paciente_id is not None
    return AgendamentoConfirmado(
        agendamento_id=agendamento.id,
        paciente_id=agendamento.paciente_id,
        dentista_id=agendamento.dentista_id,
        consultorio_id=agendamento.consultorio_id,
        procedimento_id=agendamento.procedimento_id,
        inicio=_inicio(agendamento),
        fim_periodo=_fim_periodo(agendamento),
    )


async def _hold_valido(sessao: AsyncSession, guardada: dict[str, Any]) -> bool:
    return bool(
        await sessao.scalar(
            select(
                exists().where(
                    Agendamento.id == UUID(guardada["hold_id"]),
                    Agendamento.status == "segurado",
                    Agendamento.expira_em > func.now(),
                )
            )
        )
    )


async def _inserir_hold(
    sessao: AsyncSession,
    contexto: Contexto,
    entrada: SegurarEntrada,
    horario: HorarioLivre,
    config: ConfigAgenda,
) -> Agendamento | None:
    hold = Agendamento(
        clinica_id=contexto.clinica_id,
        dentista_id=horario.dentista_id,
        consultorio_id=horario.consultorio_id,
        procedimento_id=entrada.procedimento_id,
        periodo=Range(horario.inicio, horario.fim_periodo),
        status="segurado",
        expira_em=func.now() + timedelta(minutes=config.duracao_hold_min),
        chamada_id=contexto.chamada_id,
        origem=contexto.origem,
    )
    try:
        async with sessao.begin_nested():
            sessao.add(hold)
            await sessao.flush()
    except IntegrityError as erro:
        if getattr(erro.orig, "sqlstate", None) != _SQLSTATE_EXCLUSAO:
            raise
        return None
    await sessao.refresh(hold)
    return hold


async def segurar(
    sessao: AsyncSession,
    contexto: Contexto,
    entrada: SegurarEntrada,
    chave: str,
    config: ConfigAgenda,
    publicador: PublicadorAgenda,
) -> HoldCriado:
    chave_completa = _chave(contexto, "segurar", chave)
    resposta: HoldCriado | None = None
    eventos: list[EventoAgendamento] = []
    async with _transacao(sessao):
        await travar(sessao, chave_completa)
        guardada = await idempotencia.buscar(sessao, chave_completa)
        if guardada is not None and await _hold_valido(sessao, guardada):
            return HoldCriado.model_validate(guardada)

        horario = await localizar_horario_na_grade(
            sessao,
            contexto.clinica_id,
            entrada.procedimento_id,
            entrada.dentista_id,
            entrada.inicio,
            config,
        )
        motivo = "indisponivel"
        if horario is not None:
            await travar(sessao, horario.dentista_id, horario.consultorio_id)
            expirados = await sessao.scalars(
                update(Agendamento)
                .where(
                    Agendamento.status == "segurado",
                    Agendamento.expira_em < func.now(),
                    or_(
                        Agendamento.dentista_id == horario.dentista_id,
                        Agendamento.consultorio_id == horario.consultorio_id,
                    ),
                )
                .values(status="expirado")
                .returning(Agendamento)
                .execution_options(synchronize_session=False, populate_existing=True)
            )
            eventos = [criar_evento(expirado, "expirado") for expirado in expirados]
            hold = await _inserir_hold(sessao, contexto, entrada, horario, config)
            motivo = "ocupado"
            if hold is not None:
                assert hold.expira_em is not None
                resposta = HoldCriado(
                    hold_id=hold.id,
                    expira_em=hold.expira_em,
                    dentista_id=hold.dentista_id,
                    consultorio_id=hold.consultorio_id,
                    procedimento_id=hold.procedimento_id,
                    inicio=horario.inicio,
                    fim_periodo=horario.fim_periodo,
                )
                await idempotencia.guardar(sessao, chave_completa, resposta.model_dump(mode="json"))
                eventos.append(criar_evento(hold, "criado"))

    # Depois do commit: a mensagem nunca fala de um hold que não foi gravado.
    if resposta is not None:
        # Juntas, para que o limite de publicação não some duas vezes na resposta.
        await asyncio.gather(
            publicador.hold_criado(resposta.hold_id, timedelta(minutes=config.duracao_hold_min)),
            publicador.eventos(eventos),
        )
        return resposta

    # A limpeza de holds vencidos fica gravada mesmo quando o novo hold não entra.
    await publicador.eventos(eventos)

    # Fora da transação: quem perdeu não calcula alternativas segurando os locks.
    alternativas = await listar_alternativas(
        sessao,
        contexto.clinica_id,
        entrada.procedimento_id,
        entrada.dentista_id,
        entrada.inicio,
        config,
    )
    await sessao.rollback()
    raise ErroAgenda(motivo, alternativas)


async def _recursos_do_hold(
    sessao: AsyncSession, contexto: Contexto, hold_id: UUID
) -> tuple[UUID, UUID]:
    linha = (
        await sessao.execute(
            select(Agendamento.dentista_id, Agendamento.consultorio_id).where(
                Agendamento.id == hold_id, Agendamento.clinica_id == contexto.clinica_id
            )
        )
    ).first()
    if linha is None:
        raise ErroAgenda("hold_perdido")
    return linha[0], linha[1]


async def _confirmar_hold(
    sessao: AsyncSession, contexto: Contexto, hold_id: UUID, paciente_id: UUID
) -> Agendamento:
    agendamento = await sessao.scalar(
        update(Agendamento)
        .where(
            Agendamento.id == hold_id,
            Agendamento.clinica_id == contexto.clinica_id,
            Agendamento.status == "segurado",
            Agendamento.expira_em > func.now(),
            Agendamento.origem == contexto.origem,
            Agendamento.chamada_id.is_not_distinct_from(contexto.chamada_id),
        )
        .values(status="confirmado", paciente_id=paciente_id, expira_em=None)
        .returning(Agendamento)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    if agendamento is None:
        raise ErroAgenda("hold_perdido")
    return agendamento


async def _paciente_da_confirmacao(
    sessao: AsyncSession, contexto: Contexto, entrada: ConfirmarEntrada
) -> UUID:
    if contexto.voz:
        if entrada.paciente_id is not None:
            raise ErroAgenda("paciente_nao_permitido")
        if contexto.paciente_id is None:
            raise ErroAgenda("paciente_nao_selecionado")
        return contexto.paciente_id
    if entrada.paciente_id is None:
        raise ErroAgenda("paciente_obrigatorio")
    do_paciente = await sessao.scalar(
        select(Paciente.id).where(
            Paciente.id == entrada.paciente_id, Paciente.clinica_id == contexto.clinica_id
        )
    )
    if do_paciente is None:
        raise ErroAgenda("paciente_nao_encontrado")
    return do_paciente


async def confirmar(
    sessao: AsyncSession,
    contexto: Contexto,
    hold_id: UUID,
    entrada: ConfirmarEntrada,
    chave: str,
    publicador: PublicadorAgenda,
) -> AgendamentoConfirmado:
    chave_completa = _chave(contexto, "confirmar", chave)
    async with _transacao(sessao):
        await travar(sessao, chave_completa)
        guardada = await idempotencia.buscar(sessao, chave_completa)
        if guardada is not None:
            return AgendamentoConfirmado.model_validate(guardada)

        paciente_id = await _paciente_da_confirmacao(sessao, contexto, entrada)
        await travar(sessao, *await _recursos_do_hold(sessao, contexto, hold_id))
        agendamento = await _confirmar_hold(sessao, contexto, hold_id, paciente_id)
        resposta = _confirmado(agendamento)
        await idempotencia.guardar(sessao, chave_completa, resposta.model_dump(mode="json"))
        evento = criar_evento(agendamento, "confirmado")

    await publicador.eventos([evento])
    return resposta


def _exigir_identidade(contexto: Contexto) -> None:
    if contexto.voz and not contexto.identidade_verificada:
        raise ErroAgenda("identidade_nao_verificada")


async def _consulta_alteravel(
    sessao: AsyncSession, contexto: Contexto, agendamento_id: UUID
) -> Agendamento:
    agendamento = await sessao.scalar(
        select(Agendamento).where(
            Agendamento.id == agendamento_id, Agendamento.clinica_id == contexto.clinica_id
        )
    )
    # Consulta de outro paciente responde como inexistente, para não revelar que ela existe.
    if agendamento is None or (contexto.voz and agendamento.paciente_id != contexto.paciente_id):
        raise ErroAgenda("agendamento_nao_encontrado")
    if agendamento.status != "confirmado":
        raise ErroAgenda("agendamento_nao_ativo")
    if _inicio(agendamento) <= await _agora(sessao):
        raise ErroAgenda("agendamento_passado")
    return agendamento


async def _cancelar_consulta(
    sessao: AsyncSession, agendamento_id: UUID, motivo: str | None
) -> Agendamento:
    cancelado = await sessao.scalar(
        update(Agendamento)
        .where(Agendamento.id == agendamento_id, Agendamento.status == "confirmado")
        .values(status="cancelado", motivo_cancelamento=motivo)
        .returning(Agendamento)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    if cancelado is None:
        raise ErroAgenda("agendamento_nao_ativo")
    return cancelado


async def remarcar(
    sessao: AsyncSession,
    contexto: Contexto,
    agendamento_id: UUID,
    entrada: RemarcarEntrada,
    chave: str,
    publicador: PublicadorAgenda,
) -> AgendamentoConfirmado:
    chave_completa = _chave(contexto, "remarcar", chave)
    async with _transacao(sessao):
        await travar(sessao, chave_completa)
        guardada = await idempotencia.buscar(sessao, chave_completa)
        if guardada is not None:
            return AgendamentoConfirmado.model_validate(guardada)

        _exigir_identidade(contexto)
        antigo = await _consulta_alteravel(sessao, contexto, agendamento_id)
        assert antigo.paciente_id is not None
        recursos = await _recursos_do_hold(sessao, contexto, entrada.hold_id)
        await travar(sessao, *recursos, antigo.dentista_id, antigo.consultorio_id)
        novo = await _confirmar_hold(sessao, contexto, entrada.hold_id, antigo.paciente_id)
        cancelada = await _cancelar_consulta(sessao, agendamento_id, "remarcado")
        resposta = _confirmado(novo)
        await idempotencia.guardar(sessao, chave_completa, resposta.model_dump(mode="json"))
        evento = criar_evento(novo, "remarcado", anterior=criar_evento(cancelada, "cancelado"))

    await publicador.eventos([evento])
    return resposta


async def cancelar(
    sessao: AsyncSession,
    contexto: Contexto,
    agendamento_id: UUID,
    entrada: CancelarEntrada,
    chave: str,
    publicador: PublicadorAgenda,
) -> AgendamentoCancelado:
    chave_completa = _chave(contexto, "cancelar", chave)
    async with _transacao(sessao):
        await travar(sessao, chave_completa)
        guardada = await idempotencia.buscar(sessao, chave_completa)
        if guardada is not None:
            return AgendamentoCancelado.model_validate(guardada)

        _exigir_identidade(contexto)
        agendamento = await _consulta_alteravel(sessao, contexto, agendamento_id)
        inicio = _inicio(agendamento)
        cancelada = await _cancelar_consulta(sessao, agendamento_id, entrada.motivo)
        horas = (
            await sessao.execute(
                select(Clinica.politica_cancelamento_horas).where(Clinica.id == contexto.clinica_id)
            )
        ).scalar_one()
        resposta = AgendamentoCancelado(
            agendamento_id=agendamento_id,
            dentro_do_prazo=inicio - await _agora(sessao) >= timedelta(hours=horas),
            politica_cancelamento_horas=horas,
        )
        await idempotencia.guardar(sessao, chave_completa, resposta.model_dump(mode="json"))
        evento = criar_evento(cancelada, "cancelado")

    await publicador.eventos([evento])
    return resposta
