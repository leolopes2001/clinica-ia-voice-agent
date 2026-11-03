import json
import logging
from collections.abc import Iterator

import pytest

from clinica_core.logs import configurar_logs


@pytest.fixture(autouse=True)
def restaurar_logs() -> Iterator[None]:
    raiz = logging.getLogger()
    handlers, nivel = raiz.handlers[:], raiz.level
    yield
    raiz.handlers, raiz.level = handlers, nivel
    for nome in ("httpx", "httpcore", "google_adk", "google_genai", "mcp", "uvicorn.access"):
        logging.getLogger(nome).setLevel(logging.NOTSET)


def test_cada_evento_e_uma_linha_de_json(capsys: pytest.CaptureFixture[str]) -> None:
    configurar_logs("teste")
    logging.getLogger("clinica.exemplo").info("Fila %s vazia", "holds")
    linha = json.loads(capsys.readouterr().out.strip())
    assert linha["servico"] == "teste"
    assert linha["nivel"] == "INFO"
    assert linha["logger"] == "clinica.exemplo"
    assert linha["mensagem"] == "Fila holds vazia"
    assert "quando" in linha


def test_erro_leva_o_rastro_na_mesma_linha(capsys: pytest.CaptureFixture[str]) -> None:
    configurar_logs("teste")
    try:
        raise ValueError("quebrou")
    except ValueError:
        logging.getLogger("x").exception("Falhou")
    saida = capsys.readouterr().out.strip()
    assert len(saida.splitlines()) == 1
    assert "ValueError: quebrou" in json.loads(saida)["erro"]


@pytest.mark.parametrize(
    "logger", ["httpx", "httpcore", "google_adk", "google_genai", "mcp", "uvicorn.access"]
)
def test_logs_que_trazem_dados_pessoais_ficam_de_fora(
    logger: str, capsys: pytest.CaptureFixture[str]
) -> None:
    configurar_logs("teste")
    logging.getLogger(logger).info("GET /painel/pacientes?busca=Maria")
    assert "Maria" not in capsys.readouterr().out
