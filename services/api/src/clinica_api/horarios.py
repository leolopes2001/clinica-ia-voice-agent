from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_core.config import ConfigAgenda
from clinica_core.modelos import (
    Agendamento,
    Bloqueio,
    Clinica,
    Consultorio,
    Dentista,
    GradeTrabalho,
    Procedimento,
)

PASSO = timedelta(minutes=15)
DIAS_DE_ALTERNATIVAS = timedelta(days=7)
_RESOLUCAO_TSTZ = timedelta(microseconds=1)


class ProcedimentoNaoEncontrado(Exception):
    pass


@dataclass(frozen=True)
class Turno:
    dentista_id: UUID
    consultorio_id: UUID
    inicio: datetime
    fim: datetime


@dataclass(frozen=True)
class Ocupacao:
    inicio: datetime
    fim: datetime
    # Os dois nulos = clínica toda.
    dentista_id: UUID | None = None
    consultorio_id: UUID | None = None

    def afeta(self, turno: Turno) -> bool:
        if self.dentista_id is None and self.consultorio_id is None:
            return True
        return self.dentista_id == turno.dentista_id or self.consultorio_id == turno.consultorio_id


@dataclass(frozen=True)
class HorarioLivre:
    dentista_id: UUID
    consultorio_id: UUID
    inicio: datetime
    # Fim da consulta.
    fim: datetime
    # Fim da consulta + intervalo de limpeza.
    fim_periodo: datetime


def _instante(dia: date, hora: time, fuso: ZoneInfo) -> datetime:
    # Em UTC, porque somar timedelta a um datetime com ZoneInfo ignora mudanças de horário.
    return datetime.combine(dia, hora, tzinfo=fuso).astimezone(UTC)


def expandir_grade(
    grade: Iterable[GradeTrabalho], primeiro_dia: date, ultimo_dia: date, fuso: ZoneInfo
) -> list[Turno]:
    linhas = list(grade)
    turnos: list[Turno] = []
    for deslocamento in range((ultimo_dia - primeiro_dia).days + 1):
        dia = primeiro_dia + timedelta(days=deslocamento)
        turnos.extend(
            Turno(
                linha.dentista_id,
                linha.consultorio_id,
                _instante(dia, linha.inicio, fuso),
                _instante(dia, linha.fim, fuso),
            )
            for linha in linhas
            if linha.dia_semana == dia.weekday()
        )
    return turnos


def _intervalos_livres(
    inicio: datetime, fim: datetime, ocupados: Iterable[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    livres: list[tuple[datetime, datetime]] = []
    cursor = inicio
    for ocupado_inicio, ocupado_fim in sorted(ocupados):
        if ocupado_fim <= cursor:
            continue
        if ocupado_inicio >= fim:
            break
        if ocupado_inicio > cursor:
            livres.append((cursor, ocupado_inicio))
        cursor = ocupado_fim
    if cursor < fim:
        livres.append((cursor, fim))
    return livres


def _proximo_multiplo(instante: datetime, fuso: ZoneInfo) -> datetime:
    local = instante.astimezone(fuso)
    excesso = timedelta(
        minutes=local.minute % 15, seconds=local.second, microseconds=local.microsecond
    )
    return instante + (PASSO - excesso) if excesso else instante


def calcular_horarios_livres(
    turnos: Iterable[Turno],
    ocupacoes: Sequence[Ocupacao],
    duracao: timedelta,
    limpeza: timedelta,
    fuso: ZoneInfo,
    a_partir_de: datetime,
    ate: datetime,
) -> list[HorarioLivre]:
    horarios: list[HorarioLivre] = []
    for turno in turnos:
        ocupados = [(o.inicio, o.fim) for o in ocupacoes if o.afeta(turno)]
        for livre_inicio, livre_fim in _intervalos_livres(turno.inicio, turno.fim, ocupados):
            inicio = _proximo_multiplo(livre_inicio, fuso)
            while inicio + duracao + limpeza <= livre_fim:
                if a_partir_de <= inicio <= ate:
                    horarios.append(
                        HorarioLivre(
                            turno.dentista_id,
                            turno.consultorio_id,
                            inicio,
                            inicio + duracao,
                            inicio + duracao + limpeza,
                        )
                    )
                inicio += PASSO
    return sorted(horarios, key=lambda horario: (horario.inicio, horario.dentista_id))


def _ocupacao(
    periodo: Range[datetime],
    limites: tuple[datetime, datetime],
    dentista_id: UUID | None,
    consultorio_id: UUID | None = None,
) -> Ocupacao:
    # Normaliza para [inicio, fim): um range ilimitado ou com outros limites vale tanto quanto
    # na exclusion constraint.
    inicio, fim = limites
    if periodo.lower is not None:
        inicio = periodo.lower if periodo.lower_inc else periodo.lower + _RESOLUCAO_TSTZ
    if periodo.upper is not None:
        fim = periodo.upper + _RESOLUCAO_TSTZ if periodo.upper_inc else periodo.upper
    return Ocupacao(inicio, fim, dentista_id, consultorio_id)


async def _calcular(
    sessao: AsyncSession,
    clinica_id: UUID,
    procedimento_id: UUID,
    a_partir_de: datetime,
    ate: datetime,
    config: ConfigAgenda,
    dentista_id: UUID | None,
    *,
    considerar_agendamentos: bool,
) -> list[HorarioLivre]:
    linha = (
        await sessao.execute(
            select(Procedimento, Clinica.fuso)
            .join(Clinica, Procedimento.clinica_id == Clinica.id)
            .where(Procedimento.id == procedimento_id, Procedimento.clinica_id == clinica_id)
        )
    ).first()
    if linha is None:
        raise ProcedimentoNaoEncontrado(procedimento_id)
    procedimento, nome_fuso = linha
    fuso = ZoneInfo(nome_fuso)

    # O relógio do banco é o mesmo que decide se um hold venceu.
    agora = (await sessao.execute(select(func.now()))).scalar_one()
    inicio_minimo = max(a_partir_de, agora + timedelta(minutes=config.antecedencia_minima_min))
    if inicio_minimo > ate:
        return []

    consulta_grade = (
        select(GradeTrabalho)
        .join(Dentista, GradeTrabalho.dentista_id == Dentista.id)
        .join(Consultorio, GradeTrabalho.consultorio_id == Consultorio.id)
        .where(
            GradeTrabalho.clinica_id == clinica_id,
            Dentista.ativo,
            Consultorio.ativo,
            # Lista vazia = ninguém habilitado.
            Dentista.id.in_(procedimento.dentistas_habilitados),
        )
    )
    if dentista_id is not None:
        consulta_grade = consulta_grade.where(Dentista.id == dentista_id)
    turnos = expandir_grade(
        (await sessao.scalars(consulta_grade)).all(),
        inicio_minimo.astimezone(fuso).date(),
        ate.astimezone(fuso).date(),
        fuso,
    )
    if not turnos:
        return []

    limites = (min(t.inicio for t in turnos), max(t.fim for t in turnos))
    faixa = Range(*limites)
    bloqueios = await sessao.scalars(
        select(Bloqueio).where(Bloqueio.clinica_id == clinica_id, Bloqueio.periodo.overlaps(faixa))
    )
    ocupacoes = [_ocupacao(b.periodo, limites, b.dentista_id) for b in bloqueios]
    if considerar_agendamentos:
        agendamentos = await sessao.scalars(
            select(Agendamento).where(
                Agendamento.clinica_id == clinica_id,
                Agendamento.periodo.overlaps(faixa),
                or_(
                    Agendamento.status == "confirmado",
                    # Um hold vencido fica livre mesmo antes de o worker marcá-lo como expirado.
                    and_(Agendamento.status == "segurado", Agendamento.expira_em > func.now()),
                ),
            )
        )
        ocupacoes += [
            _ocupacao(a.periodo, limites, a.dentista_id, a.consultorio_id) for a in agendamentos
        ]
    return calcular_horarios_livres(
        turnos,
        ocupacoes,
        timedelta(minutes=procedimento.duracao_min),
        timedelta(minutes=procedimento.intervalo_limpeza_min),
        fuso,
        inicio_minimo,
        ate,
    )


async def listar_horarios_livres(
    sessao: AsyncSession,
    clinica_id: UUID,
    procedimento_id: UUID,
    a_partir_de: datetime,
    ate: datetime,
    config: ConfigAgenda,
    dentista_id: UUID | None = None,
) -> list[HorarioLivre]:
    return await _calcular(
        sessao,
        clinica_id,
        procedimento_id,
        a_partir_de,
        ate,
        config,
        dentista_id,
        considerar_agendamentos=True,
    )


async def localizar_horario_na_grade(
    sessao: AsyncSession,
    clinica_id: UUID,
    procedimento_id: UUID,
    dentista_id: UUID,
    inicio: datetime,
    config: ConfigAgenda,
) -> HorarioLivre | None:
    """Valida tudo que a exclusion constraint não conhece, ignorando os agendamentos."""
    horarios = await _calcular(
        sessao,
        clinica_id,
        procedimento_id,
        inicio,
        inicio,
        config,
        dentista_id,
        considerar_agendamentos=False,
    )
    return horarios[0] if horarios else None


async def listar_alternativas(
    sessao: AsyncSession,
    clinica_id: UUID,
    procedimento_id: UUID,
    dentista_id: UUID,
    inicio: datetime,
    config: ConfigAgenda,
) -> list[HorarioLivre]:
    nome_fuso = (
        await sessao.execute(select(Clinica.fuso).where(Clinica.id == clinica_id))
    ).scalar_one()
    dia = inicio.astimezone(ZoneInfo(nome_fuso)).replace(hour=0, minute=0, second=0, microsecond=0)
    return await listar_horarios_livres(
        sessao,
        clinica_id,
        procedimento_id,
        dia,
        (dia + DIAS_DE_ALTERNATIVAS).replace(hour=23, minute=59),
        config,
        dentista_id,
    )
