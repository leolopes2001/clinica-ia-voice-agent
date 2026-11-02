from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from clinica_api.horarios import HorarioLivre
from clinica_api.voz import escolher_variados, falar_horario, normalizar, turno_de

FUSO = ZoneInfo("America/Sao_Paulo")
DENTISTA_A = uuid4()
DENTISTA_B = uuid4()


def _em(dia: int, hora: int, minuto: int = 0) -> datetime:
    return datetime(2031, 1, dia, hora, minuto, tzinfo=FUSO).astimezone(UTC)


def _horario(inicio: datetime, dentista: object = DENTISTA_A) -> HorarioLivre:
    fim = inicio + timedelta(minutes=30)
    return HorarioLivre(dentista, uuid4(), inicio, fim, fim)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("hora", "minuto", "turno"),
    [
        (8, 0, "manha"),
        (11, 45, "manha"),
        (12, 0, "tarde"),
        (17, 45, "tarde"),
        (18, 0, "noite"),
        (20, 30, "noite"),
    ],
)
def test_turno_pelo_inicio_no_fuso_da_clinica(hora: int, minuto: int, turno: str) -> None:
    assert turno_de(_em(6, hora, minuto), FUSO) == turno


@pytest.mark.parametrize(
    ("inicio", "fala"),
    [
        (_em(9, 14, 30), "quinta, dia 9, às 14h30"),
        (_em(6, 9), "segunda, dia 6, às 9h"),
        (_em(11, 8, 15), "sábado, dia 11, às 8h15"),
        (_em(12, 18), "domingo, dia 12, às 18h"),
    ],
)
def test_fala_natural_no_fuso_da_clinica(inicio: datetime, fala: str) -> None:
    assert falar_horario(inicio, FUSO) == fala


def test_no_maximo_tres_opcoes_em_ordem_cronologica() -> None:
    candidatos = [_horario(_em(6, 8) + timedelta(minutes=15 * i)) for i in range(20)]
    escolhidos = escolher_variados(candidatos, FUSO)
    assert len(escolhidos) == 3
    assert [e.inicio for e in escolhidos] == sorted(e.inicio for e in escolhidos)


def test_opcoes_comecam_pelo_mais_cedo_e_variam_o_dia() -> None:
    candidatos = [_horario(_em(dia, hora)) for dia in (6, 7, 8) for hora in (8, 9, 10)]
    escolhidos = escolher_variados(candidatos, FUSO)
    assert escolhidos[0].inicio == _em(6, 8)
    assert {e.inicio.astimezone(FUSO).day for e in escolhidos} == {6, 7, 8}


def test_opcoes_variam_o_turno_no_mesmo_dia() -> None:
    candidatos = [_horario(_em(6, hora)) for hora in (8, 9, 10, 14, 15, 19)]
    escolhidos = escolher_variados(candidatos, FUSO)
    assert {turno_de(e.inicio, FUSO) for e in escolhidos} == {"manha", "tarde", "noite"}


def test_opcoes_variam_o_dentista_no_mesmo_horario() -> None:
    candidatos = [_horario(_em(6, 8)), _horario(_em(6, 8), DENTISTA_B)]
    escolhidos = escolher_variados(candidatos, FUSO)
    assert {e.dentista_id for e in escolhidos} == {DENTISTA_A, DENTISTA_B}


def test_opcoes_espacadas_quando_so_ha_um_dia_e_um_turno() -> None:
    candidatos = [_horario(_em(6, 8) + timedelta(minutes=15 * i)) for i in range(12)]
    inicios = [e.inicio for e in escolher_variados(candidatos, FUSO)]
    assert inicios == [_em(6, 8), _em(6, 9), _em(6, 10)]


def test_opcoes_o_mais_longe_possivel_quando_a_janela_e_curta() -> None:
    candidatos = [_horario(_em(6, 8) + timedelta(minutes=15 * i)) for i in range(8)]
    inicios = [e.inicio for e in escolher_variados(candidatos, FUSO)]
    assert inicios == [_em(6, 8), _em(6, 9), _em(6, 9, 45)]


def test_menos_candidatos_que_o_limite() -> None:
    assert len(escolher_variados([_horario(_em(6, 8))], FUSO)) == 1
    assert escolher_variados([], FUSO) == []


def test_normalizar_ignora_acento_caixa_e_espacos() -> None:
    assert normalizar("  AVALIAÇÃO   Clínica ") == "avaliacao clinica"
