from pathlib import Path

import pytest
from pydantic import ValidationError

from clinica_core.config import ConfigAgenda, ConfigPostgres, ConfigRabbit

VARIAVEIS = [
    f"{prefixo}_{nome}"
    for prefixo, nomes in {
        "POSTGRES": ("HOST", "PORT", "USER", "PASSWORD", "DB"),
        "RABBITMQ": ("HOST", "PORT", "USER", "PASSWORD", "VHOST"),
        "AGENDA": ("ANTECEDENCIA_MINIMA_MIN", "DURACAO_HOLD_MIN"),
    }.items()
    for nome in nomes
]


@pytest.fixture(autouse=True)
def ambiente_limpo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for variavel in VARIAVEIS:
        monkeypatch.delenv(variavel, raising=False)


def test_padroes_batem_com_o_compose() -> None:
    assert ConfigPostgres().url == "postgresql+asyncpg://clinica:clinica@localhost:5432/clinica"
    assert ConfigRabbit().url == "amqp://clinica:clinica@localhost:5672/%2F"


def test_variavel_de_ambiente_sobrescreve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    monkeypatch.setenv("RABBITMQ_HOST", "rabbitmq")
    monkeypatch.setenv("RABBITMQ_VHOST", "teste")
    assert ConfigPostgres().host == "postgres"
    assert ConfigRabbit().url == "amqp://clinica:clinica@rabbitmq:5672/teste"


def test_arquivo_env_e_lido(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("POSTGRES_DB=outro\nRABBITMQ_PORT=5673\nAPI_PORT=8001\n")
    assert ConfigPostgres().db == "outro"
    assert ConfigRabbit().port == 5673


def test_url_escapa_caracteres_especiais(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "a@b/c:d")
    monkeypatch.setenv("RABBITMQ_PASSWORD", "a@b/c:d")
    assert ":a%40b%2Fc%3Ad@" in ConfigPostgres().url
    assert ":a%40b%2Fc%3Ad@" in ConfigRabbit().url


def test_senha_nao_aparece_no_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "segredo")
    monkeypatch.setenv("RABBITMQ_PASSWORD", "segredo")
    assert "segredo" not in repr(ConfigPostgres())
    assert "segredo" not in repr(ConfigRabbit())


def test_antecedencia_minima_padrao_e_de_120_minutos() -> None:
    assert ConfigAgenda().antecedencia_minima_min == 120


def test_antecedencia_minima_vem_do_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENDA_ANTECEDENCIA_MINIMA_MIN", "30")
    assert ConfigAgenda().antecedencia_minima_min == 30


def test_antecedencia_minima_negativa_e_recusada(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENDA_ANTECEDENCIA_MINIMA_MIN", "-1")
    with pytest.raises(ValidationError):
        ConfigAgenda()


def test_duracao_do_hold_padrao_e_de_3_minutos() -> None:
    assert ConfigAgenda().duracao_hold_min == 3


def test_duracao_do_hold_zero_e_recusada(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENDA_DURACAO_HOLD_MIN", "0")
    with pytest.raises(ValidationError):
        ConfigAgenda()
