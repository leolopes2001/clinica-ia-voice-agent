from datetime import date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from clinica_api.horarios import (
    HorarioLivre,
    Ocupacao,
    Turno,
    calcular_horarios_livres,
    expandir_grade,
)
from clinica_core.modelos import GradeTrabalho

FUSO = ZoneInfo("America/Sao_Paulo")
# Uma segunda-feira.
DIA = date(2026, 12, 7)
DENTISTA_A = UUID(int=1)
DENTISTA_B = UUID(int=2)
CONSULTORIO_1 = UUID(int=11)
CONSULTORIO_2 = UUID(int=12)


def _h(hora: int, minuto: int = 0, dia: date = DIA) -> datetime:
    return datetime(dia.year, dia.month, dia.day, hora, minuto, tzinfo=FUSO)


def _turno(
    inicio: datetime,
    fim: datetime,
    dentista: UUID = DENTISTA_A,
    consultorio: UUID = CONSULTORIO_1,
) -> Turno:
    return Turno(dentista, consultorio, inicio, fim)


def _calcular(
    turnos: list[Turno],
    ocupacoes: list[Ocupacao] | None = None,
    duracao: int = 30,
    limpeza: int = 0,
    a_partir_de: datetime | None = None,
    ate: datetime | None = None,
) -> list[HorarioLivre]:
    return calcular_horarios_livres(
        turnos,
        ocupacoes or [],
        timedelta(minutes=duracao),
        timedelta(minutes=limpeza),
        FUSO,
        a_partir_de or _h(0),
        ate or _h(23, 59),
    )


def _inicios(horarios: list[HorarioLivre]) -> list[str]:
    return [h.inicio.astimezone(FUSO).strftime("%H:%M") for h in horarios]


def test_candidatos_comecam_a_cada_15_minutos() -> None:
    horarios = _calcular([_turno(_h(8), _h(9))])
    assert _inicios(horarios) == ["08:00", "08:15", "08:30"]


def test_passo_nao_depende_da_duracao_nem_da_limpeza() -> None:
    horarios = _calcular([_turno(_h(8), _h(10))], duracao=30, limpeza=10)
    assert _inicios(horarios) == ["08:00", "08:15", "08:30", "08:45", "09:00", "09:15"]


def test_turno_fora_do_multiplo_arredonda_para_cima() -> None:
    assert _inicios(_calcular([_turno(_h(8, 10), _h(9))])) == ["08:15", "08:30"]


def test_apos_consulta_quebrada_arredonda_para_cima() -> None:
    ocupacao = Ocupacao(_h(8), _h(9, 40), DENTISTA_A, CONSULTORIO_1)
    horarios = _calcular([_turno(_h(8), _h(11))], [ocupacao])
    assert _inicios(horarios) == ["09:45", "10:00", "10:15", "10:30"]


def test_horario_devolve_fim_da_consulta_e_do_periodo() -> None:
    horario = _calcular([_turno(_h(8), _h(8, 40))], duracao=30, limpeza=10)[0]
    assert horario.dentista_id == DENTISTA_A
    assert horario.consultorio_id == CONSULTORIO_1
    assert horario.inicio == _h(8)
    assert horario.fim == _h(8, 30)
    assert horario.fim_periodo == _h(8, 40)


def test_procedimento_que_cabe_exatamente_no_fim_do_turno() -> None:
    assert _inicios(_calcular([_turno(_h(17), _h(18))], duracao=60)) == ["17:00"]


def test_procedimento_que_nao_cabe_no_fim_do_turno() -> None:
    assert _calcular([_turno(_h(17, 30), _h(18))], duracao=60) == []


def test_procedimento_que_so_nao_cabe_por_causa_da_limpeza() -> None:
    turno = _turno(_h(17, 30), _h(18))
    assert _inicios(_calcular([turno], duracao=30)) == ["17:30"]
    assert _calcular([turno], duracao=30, limpeza=1) == []


def test_almoco_como_bloqueio_do_dentista() -> None:
    almoco = Ocupacao(_h(12), _h(13), DENTISTA_A)
    horarios = _calcular([_turno(_h(11), _h(14))], [almoco], duracao=60)
    assert _inicios(horarios) == ["11:00", "13:00"]


def test_almoco_como_intervalo_entre_duas_linhas_de_grade() -> None:
    turnos = [_turno(_h(11), _h(12)), _turno(_h(13), _h(14))]
    assert _inicios(_calcular(turnos, duracao=60)) == ["11:00", "13:00"]


def test_procedimento_nao_atravessa_o_almoco() -> None:
    almoco = Ocupacao(_h(12), _h(13), DENTISTA_A)
    horarios = _calcular([_turno(_h(11, 30), _h(13, 30))], [almoco], duracao=45)
    assert horarios == []


def test_feriado_bloqueia_todos_os_dentistas() -> None:
    feriado = Ocupacao(_h(0), _h(0, dia=DIA + timedelta(days=1)))
    turnos = [_turno(_h(8), _h(12)), _turno(_h(8), _h(12), DENTISTA_B, CONSULTORIO_2)]
    assert _calcular(turnos, [feriado]) == []


def test_ferias_bloqueiam_so_o_dentista() -> None:
    ferias = Ocupacao(_h(0), _h(0, dia=DIA + timedelta(days=30)), DENTISTA_A)
    turnos = [_turno(_h(8), _h(9)), _turno(_h(8), _h(9), DENTISTA_B, CONSULTORIO_2)]
    assert {h.dentista_id for h in _calcular(turnos, [ferias])} == {DENTISTA_B}


def test_ocupacao_atravessando_o_inicio_e_o_fim_do_turno() -> None:
    ocupacoes = [
        Ocupacao(_h(7), _h(8, 30), DENTISTA_A),
        Ocupacao(_h(9, 30), _h(11), DENTISTA_A),
    ]
    assert _inicios(_calcular([_turno(_h(8), _h(10))], ocupacoes)) == ["08:30", "08:45", "09:00"]


def test_ocupacoes_sobrepostas_e_fora_do_turno() -> None:
    ocupacoes = [
        Ocupacao(_h(9), _h(10), DENTISTA_A),
        Ocupacao(_h(9, 30), _h(9, 45), DENTISTA_A),
        Ocupacao(_h(15), _h(16), DENTISTA_A),
    ]
    assert _inicios(_calcular([_turno(_h(9), _h(10, 30))], ocupacoes)) == ["10:00"]


def test_agendamento_de_outro_dentista_no_mesmo_consultorio_bloqueia() -> None:
    agendamento = Ocupacao(_h(8), _h(9), DENTISTA_B, CONSULTORIO_1)
    assert _inicios(_calcular([_turno(_h(8), _h(9, 30))], [agendamento])) == ["09:00"]


def test_agendamento_do_dentista_em_outro_consultorio_bloqueia() -> None:
    agendamento = Ocupacao(_h(8), _h(9), DENTISTA_A, CONSULTORIO_2)
    assert _inicios(_calcular([_turno(_h(8), _h(9, 30))], [agendamento])) == ["09:00"]


def test_ocupacao_alheia_nao_afeta() -> None:
    agendamento = Ocupacao(_h(8), _h(9), DENTISTA_B, CONSULTORIO_2)
    assert _inicios(_calcular([_turno(_h(8), _h(8, 30))], [agendamento])) == ["08:00"]


def test_janela_inclui_as_duas_pontas() -> None:
    horarios = _calcular([_turno(_h(8), _h(12))], a_partir_de=_h(9), ate=_h(9, 30))
    assert _inicios(horarios) == ["09:00", "09:15", "09:30"]


def test_a_janela_nao_muda_os_candidatos() -> None:
    horarios = _calcular([_turno(_h(8), _h(12))], a_partir_de=_h(9, 7), ate=_h(9, 30))
    assert _inicios(horarios) == ["09:15", "09:30"]


def test_resultado_em_ordem_cronologica_com_varios_dentistas() -> None:
    turnos = [
        _turno(_h(9), _h(9, 30), DENTISTA_B, CONSULTORIO_2),
        _turno(_h(8), _h(8, 30), DENTISTA_B, CONSULTORIO_2),
        _turno(_h(9), _h(9, 30), DENTISTA_A, CONSULTORIO_1),
    ]
    assert [(h.inicio, h.dentista_id) for h in _calcular(turnos)] == [
        (_h(8), DENTISTA_B),
        (_h(9), DENTISTA_A),
        (_h(9), DENTISTA_B),
    ]


def _linha(dia_semana: int, inicio: time = time(8), fim: time = time(12)) -> GradeTrabalho:
    return GradeTrabalho(
        clinica_id=uuid4(),
        dentista_id=DENTISTA_A,
        consultorio_id=CONSULTORIO_1,
        dia_semana=dia_semana,
        inicio=inicio,
        fim=fim,
    )


def test_dia_sem_grade_nao_gera_turno() -> None:
    domingo = DIA - timedelta(days=1)
    assert expandir_grade([_linha(0)], domingo, domingo, FUSO) == []


def test_grade_usa_o_dia_da_semana_local() -> None:
    # Domingo às 22h em São Paulo já é segunda-feira em UTC.
    domingo = DIA - timedelta(days=1)
    turnos = expandir_grade([_linha(6, time(22), time(23))], domingo, domingo, FUSO)
    assert [t.inicio for t in turnos] == [_h(22, dia=domingo)]
    assert turnos[0].inicio.weekday() == 0


def test_grade_cobre_todos_os_dias_do_intervalo() -> None:
    turnos = expandir_grade([_linha(0), _linha(2)], DIA, DIA + timedelta(days=7), FUSO)
    assert [t.inicio for t in turnos] == [
        _h(8),
        _h(8, dia=DIA + timedelta(days=2)),
        _h(8, dia=DIA + timedelta(days=7)),
    ]


def test_grade_respeita_mudanca_de_horario_do_fuso() -> None:
    # Nova York muda de UTC-5 para UTC-4 em 8/3/2026, um domingo.
    fuso = ZoneInfo("America/New_York")
    turnos = expandir_grade([_linha(6, time(1), time(5))], date(2026, 3, 8), date(2026, 3, 8), fuso)
    assert turnos[0].fim - turnos[0].inicio == timedelta(hours=3)
    assert turnos[0].inicio.astimezone(fuso).hour == 1
    assert turnos[0].fim.astimezone(fuso).hour == 5
