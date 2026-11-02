from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_api.horarios import (
    HorarioLivre,
    ProcedimentoNaoEncontrado,
    listar_horarios_livres,
)
from clinica_core.config import ConfigAgenda
from clinica_core.modelos import (
    Agendamento,
    Bloqueio,
    Clinica,
    Consultorio,
    Dentista,
    GradeTrabalho,
    Paciente,
    Procedimento,
)

FUSO = ZoneInfo("America/Sao_Paulo")
SEM_ANTECEDENCIA = ConfigAgenda(antecedencia_minima_min=0)


@dataclass
class Cenario:
    clinica_id: UUID
    dentista_a: UUID
    dentista_b: UUID
    consultorio_1: UUID
    consultorio_2: UUID
    procedimento_id: UUID
    # Uma segunda-feira futura.
    dia: date

    def h(self, hora: int, minuto: int = 0) -> datetime:
        return datetime.combine(self.dia, time(hora, minuto), tzinfo=FUSO)


async def _agora(sessao: AsyncSession) -> datetime:
    return (await sessao.execute(select(func.now()))).scalar_one()


def _grade(c: Cenario, dentista: UUID, consultorio: UUID, inicio: time, fim: time) -> GradeTrabalho:
    return GradeTrabalho(
        clinica_id=c.clinica_id,
        dentista_id=dentista,
        consultorio_id=consultorio,
        dia_semana=c.dia.weekday(),
        inicio=inicio,
        fim=fim,
    )


@pytest.fixture
async def cenario(sessao: AsyncSession) -> Cenario:
    clinica = Clinica(nome="C", telefone="+5511", endereco="R", politica_cancelamento_horas=24)
    sessao.add(clinica)
    await sessao.flush()
    dentista_a = Dentista(clinica_id=clinica.id, nome="A")
    dentista_b = Dentista(clinica_id=clinica.id, nome="B")
    consultorio_1 = Consultorio(clinica_id=clinica.id, nome="1")
    consultorio_2 = Consultorio(clinica_id=clinica.id, nome="2")
    sessao.add_all([dentista_a, dentista_b, consultorio_1, consultorio_2])
    await sessao.flush()
    procedimento = Procedimento(
        clinica_id=clinica.id,
        nome="Avaliação",
        duracao_min=30,
        dentistas_habilitados=[dentista_a.id, dentista_b.id],
    )
    sessao.add(procedimento)
    await sessao.flush()
    hoje = (await _agora(sessao)).astimezone(FUSO).date()
    c = Cenario(
        clinica.id,
        dentista_a.id,
        dentista_b.id,
        consultorio_1.id,
        consultorio_2.id,
        procedimento.id,
        hoje + timedelta(days=14 - hoje.weekday()),
    )
    sessao.add(_grade(c, c.dentista_a, c.consultorio_1, time(8), time(10)))
    await sessao.flush()
    return c


async def _listar(
    sessao: AsyncSession,
    c: Cenario,
    dentista: UUID | None = None,
    config: ConfigAgenda = SEM_ANTECEDENCIA,
) -> list[HorarioLivre]:
    return await listar_horarios_livres(
        sessao, c.clinica_id, c.procedimento_id, c.h(0), c.h(23, 59), config, dentista
    )


def _inicios(horarios: list[HorarioLivre]) -> list[str]:
    return [h.inicio.astimezone(FUSO).strftime("%H:%M") for h in horarios]


def _agendamento(
    c: Cenario,
    inicio: datetime,
    fim: datetime,
    dentista: UUID | None = None,
    consultorio: UUID | None = None,
    **campos: Any,
) -> Agendamento:
    padrao: dict[str, Any] = {
        "status": "segurado",
        "expira_em": datetime.now(UTC) + timedelta(minutes=3),
        "origem": "voz",
    }
    return Agendamento(
        clinica_id=c.clinica_id,
        dentista_id=dentista or c.dentista_a,
        consultorio_id=consultorio or c.consultorio_1,
        procedimento_id=c.procedimento_id,
        periodo=Range(inicio, fim),
        **{**padrao, **campos},
    )


async def _gravar(sessao: AsyncSession, *objetos: Any) -> None:
    sessao.add_all(objetos)
    await sessao.flush()


async def test_lista_em_multiplos_de_15_minutos(sessao: AsyncSession, cenario: Cenario) -> None:
    horarios = await _listar(sessao, cenario)
    assert _inicios(horarios) == [
        "08:00",
        "08:15",
        "08:30",
        "08:45",
        "09:00",
        "09:15",
        "09:30",
    ]
    assert {h.consultorio_id for h in horarios} == {cenario.consultorio_1}


async def test_hold_ativo_ocupa(sessao: AsyncSession, cenario: Cenario) -> None:
    await _gravar(sessao, _agendamento(cenario, cenario.h(8), cenario.h(9, 30)))
    assert _inicios(await _listar(sessao, cenario)) == ["09:30"]


async def test_hold_vencido_nao_ocupa(sessao: AsyncSession, cenario: Cenario) -> None:
    vencido = await _agora(sessao) - timedelta(seconds=1)
    await _gravar(sessao, _agendamento(cenario, cenario.h(8), cenario.h(9, 30), expira_em=vencido))
    assert "08:00" in _inicios(await _listar(sessao, cenario))


async def test_confirmado_ocupa(sessao: AsyncSession, cenario: Cenario) -> None:
    paciente = Paciente(
        clinica_id=cenario.clinica_id,
        nome="Maria",
        telefone_e164="+5511999990000",
        data_nascimento=date(1990, 1, 1),
    )
    await _gravar(sessao, paciente)
    await _gravar(
        sessao,
        _agendamento(
            cenario,
            cenario.h(8),
            cenario.h(9, 30),
            status="confirmado",
            expira_em=None,
            paciente_id=paciente.id,
        ),
    )
    assert _inicios(await _listar(sessao, cenario)) == ["09:30"]


@pytest.mark.parametrize("status", ["cancelado", "expirado", "faltou", "concluido"])
async def test_status_inativo_nao_ocupa(
    sessao: AsyncSession, cenario: Cenario, status: str
) -> None:
    await _gravar(
        sessao, _agendamento(cenario, cenario.h(8), cenario.h(9, 30), status=status, expira_em=None)
    )
    assert "08:00" in _inicios(await _listar(sessao, cenario))


async def test_consultorio_ocupado_por_outro_dentista(
    sessao: AsyncSession, cenario: Cenario
) -> None:
    await _gravar(
        sessao, _agendamento(cenario, cenario.h(8), cenario.h(9, 30), dentista=cenario.dentista_b)
    )
    assert _inicios(await _listar(sessao, cenario)) == ["09:30"]


async def test_dentista_ocupado_em_outro_consultorio(
    sessao: AsyncSession, cenario: Cenario
) -> None:
    await _gravar(
        sessao,
        _agendamento(cenario, cenario.h(8), cenario.h(9, 30), consultorio=cenario.consultorio_2),
    )
    assert _inicios(await _listar(sessao, cenario)) == ["09:30"]


async def test_limpeza_de_agendamento_existente_ocupa(
    sessao: AsyncSession, cenario: Cenario
) -> None:
    await _gravar(sessao, _agendamento(cenario, cenario.h(8), cenario.h(9, 25)))
    assert _inicios(await _listar(sessao, cenario)) == ["09:30"]


async def test_feriado_da_clinica(sessao: AsyncSession, cenario: Cenario) -> None:
    feriado = Bloqueio(
        clinica_id=cenario.clinica_id,
        periodo=Range(cenario.h(0), cenario.h(23, 59)),
        motivo="Feriado",
    )
    await _gravar(sessao, feriado)
    assert await _listar(sessao, cenario) == []


async def test_bloqueio_de_outro_dentista_nao_afeta(sessao: AsyncSession, cenario: Cenario) -> None:
    ferias = Bloqueio(
        clinica_id=cenario.clinica_id,
        dentista_id=cenario.dentista_b,
        periodo=Range(cenario.h(0), cenario.h(23, 59)),
        motivo="Férias",
    )
    await _gravar(sessao, ferias)
    assert len(await _listar(sessao, cenario)) == 7


async def test_bloqueio_sem_data_de_fim(sessao: AsyncSession, cenario: Cenario) -> None:
    afastamento = Bloqueio(
        clinica_id=cenario.clinica_id,
        dentista_id=cenario.dentista_a,
        periodo=Range(cenario.h(9), None),
        motivo="Afastamento",
    )
    await _gravar(sessao, afastamento)
    assert _inicios(await _listar(sessao, cenario)) == ["08:00", "08:15", "08:30"]


async def test_bloqueio_com_limite_superior_inclusivo(
    sessao: AsyncSession, cenario: Cenario
) -> None:
    bloqueio = Bloqueio(
        clinica_id=cenario.clinica_id,
        dentista_id=cenario.dentista_a,
        periodo=Range(cenario.h(8), cenario.h(9, 15), bounds="[]"),
        motivo="Reunião",
    )
    await _gravar(sessao, bloqueio)
    assert _inicios(await _listar(sessao, cenario)) == ["09:30"]


async def test_habilitados_vazio_significa_ninguem(sessao: AsyncSession, cenario: Cenario) -> None:
    procedimento = await sessao.get_one(Procedimento, cenario.procedimento_id)
    procedimento.dentistas_habilitados = []
    await sessao.flush()
    assert await _listar(sessao, cenario) == []


async def test_dentista_nao_habilitado_fica_de_fora(sessao: AsyncSession, cenario: Cenario) -> None:
    procedimento = await sessao.get_one(Procedimento, cenario.procedimento_id)
    procedimento.dentistas_habilitados = [cenario.dentista_b]
    await sessao.flush()
    assert await _listar(sessao, cenario) == []


async def test_dentista_inativo_fica_de_fora(sessao: AsyncSession, cenario: Cenario) -> None:
    dentista = await sessao.get_one(Dentista, cenario.dentista_a)
    dentista.ativo = False
    await sessao.flush()
    assert await _listar(sessao, cenario) == []


async def test_consultorio_inativo_fica_de_fora(sessao: AsyncSession, cenario: Cenario) -> None:
    consultorio = await sessao.get_one(Consultorio, cenario.consultorio_1)
    consultorio.ativo = False
    await sessao.flush()
    assert await _listar(sessao, cenario) == []


async def test_filtro_por_dentista(sessao: AsyncSession, cenario: Cenario) -> None:
    sessao.add(_grade(cenario, cenario.dentista_b, cenario.consultorio_2, time(14), time(15)))
    await sessao.flush()
    todos = await _listar(sessao, cenario)
    assert {h.dentista_id for h in todos} == {cenario.dentista_a, cenario.dentista_b}
    so_b = await _listar(sessao, cenario, cenario.dentista_b)
    assert _inicios(so_b) == ["14:00", "14:15", "14:30"]


async def test_procedimento_de_outra_clinica_e_recusado(
    sessao: AsyncSession, cenario: Cenario
) -> None:
    with pytest.raises(ProcedimentoNaoEncontrado):
        await listar_horarios_livres(
            sessao, uuid4(), cenario.procedimento_id, cenario.h(0), cenario.h(1), SEM_ANTECEDENCIA
        )


async def test_ocupacao_de_outra_clinica_nao_afeta(sessao: AsyncSession, cenario: Cenario) -> None:
    outra = Clinica(nome="O", telefone="+5521", endereco="R", politica_cancelamento_horas=24)
    await _gravar(sessao, outra)
    feriado = Bloqueio(
        clinica_id=outra.id, periodo=Range(cenario.h(0), cenario.h(23, 59)), motivo="Feriado"
    )
    await _gravar(sessao, feriado)
    assert len(await _listar(sessao, cenario)) == 7


async def _grade_o_dia_todo(sessao: AsyncSession, c: Cenario) -> None:
    sessao.add_all(
        GradeTrabalho(
            clinica_id=c.clinica_id,
            dentista_id=c.dentista_b,
            consultorio_id=c.consultorio_2,
            dia_semana=dia,
            inicio=time(0),
            fim=time(23, 59),
        )
        for dia in range(7)
    )
    await sessao.flush()


async def test_antecedencia_minima_descarta_horarios_proximos(
    sessao: AsyncSession, cenario: Cenario
) -> None:
    await _grade_o_dia_todo(sessao, cenario)
    agora = await _agora(sessao)
    horarios = await listar_horarios_livres(
        sessao,
        cenario.clinica_id,
        cenario.procedimento_id,
        agora,
        agora + timedelta(hours=6),
        ConfigAgenda(antecedencia_minima_min=120),
        cenario.dentista_b,
    )
    assert horarios
    limite = agora + timedelta(minutes=120)
    assert all(h.inicio >= limite for h in horarios)
    assert horarios[0].inicio - limite < timedelta(minutes=15)


async def test_sem_antecedencia_nao_devolve_horario_passado(
    sessao: AsyncSession, cenario: Cenario
) -> None:
    await _grade_o_dia_todo(sessao, cenario)
    agora = await _agora(sessao)
    horarios = await listar_horarios_livres(
        sessao,
        cenario.clinica_id,
        cenario.procedimento_id,
        agora - timedelta(hours=6),
        agora + timedelta(hours=6),
        SEM_ANTECEDENCIA,
        cenario.dentista_b,
    )
    assert horarios
    assert all(h.inicio >= agora for h in horarios)


async def test_janela_toda_dentro_da_antecedencia_nao_devolve_nada(
    sessao: AsyncSession, cenario: Cenario
) -> None:
    await _grade_o_dia_todo(sessao, cenario)
    agora = await _agora(sessao)
    horarios = await listar_horarios_livres(
        sessao,
        cenario.clinica_id,
        cenario.procedimento_id,
        agora,
        agora + timedelta(minutes=119),
        ConfigAgenda(antecedencia_minima_min=120),
    )
    assert horarios == []


async def _insercao_recusada(sessao: AsyncSession, c: Cenario, h: HorarioLivre) -> bool:
    try:
        async with sessao.begin_nested() as ponto:
            await _gravar(
                sessao,
                _agendamento(c, h.inicio, h.fim_periodo, h.dentista_id, h.consultorio_id),
            )
            await ponto.rollback()
    except DBAPIError as erro:
        assert getattr(erro.orig, "sqlstate", None) == "23P01"
        return True
    return False


async def test_calculo_concorda_com_as_exclusion_constraints(
    sessao: AsyncSession, cenario: Cenario
) -> None:
    # Dentista A: ocupado por ele mesmo noutro consultório e pelo B no consultório dele.
    await _gravar(
        sessao,
        _agendamento(cenario, cenario.h(8), cenario.h(8, 25), consultorio=cenario.consultorio_2),
        _agendamento(cenario, cenario.h(9), cenario.h(9, 10), dentista=cenario.dentista_b),
    )
    livres = await _listar(sessao, cenario)
    assert livres
    for horario in livres:
        assert not await _insercao_recusada(sessao, cenario, horario)

    inicios_livres = {h.inicio for h in livres}
    duracao = timedelta(minutes=30)
    for minutos in range(0, 91, 15):
        inicio = cenario.h(8) + timedelta(minutes=minutos)
        if inicio in inicios_livres:
            continue
        ocupado = HorarioLivre(
            cenario.dentista_a, cenario.consultorio_1, inicio, inicio + duracao, inicio + duracao
        )
        assert await _insercao_recusada(sessao, cenario, ocupado)
