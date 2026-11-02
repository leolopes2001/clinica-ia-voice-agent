import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

RAIZ = Path(__file__).resolve().parent.parent
IMAGENS = {
    "postgres": "pgvector/pgvector:pg16",
    "rabbitmq": "rabbitmq:3-management",
    "redis": "redis:7",
}
CONSTRUIDOS = ["api"]
PORTAS = {
    "postgres": {("127.0.0.1", "5432")},
    "rabbitmq": {("127.0.0.1", "5672"), ("127.0.0.1", "15672")},
    "redis": set[tuple[str, str]](),
}


def _compose(*args: str) -> str:
    resultado = subprocess.run(
        ["docker", "compose", *args], cwd=RAIZ, capture_output=True, text=True, check=True
    )
    return resultado.stdout


@pytest.fixture(scope="module")
def configuracao() -> dict[str, Any]:
    if shutil.which("docker") is None:
        pytest.skip("docker não instalado")
    servicos: dict[str, Any] = json.loads(_compose("config", "--format", "json"))["services"]
    return servicos


def _servicos_em_execucao() -> dict[str, dict[str, Any]]:
    if shutil.which("docker") is None:
        pytest.skip("docker não instalado")
    try:
        saida = _compose("ps", "--format", "json")
    except subprocess.CalledProcessError:
        pytest.skip("daemon do docker indisponível")
    linhas = [json.loads(linha) for linha in saida.splitlines() if linha.strip()]
    if not linhas:
        pytest.skip("stack parada: rode `docker compose up -d --wait`")
    return {linha["Service"]: linha for linha in linhas}


def test_servicos_e_imagens(configuracao: dict[str, Any]) -> None:
    assert set(configuracao) == {*IMAGENS, *CONSTRUIDOS}
    assert {nome: configuracao[nome]["image"] for nome in IMAGENS} == IMAGENS


@pytest.mark.parametrize("servico", CONSTRUIDOS)
def test_servico_construido_no_repositorio(configuracao: dict[str, Any], servico: str) -> None:
    assert configuracao[servico]["build"]["context"] == str(RAIZ)
    assert configuracao[servico]["build"]["dockerfile"] == f"services/{servico}/Dockerfile"


def test_api_publicada_so_em_loopback(configuracao: dict[str, Any]) -> None:
    portas = configuracao["api"]["ports"]
    assert [(porta.get("host_ip"), porta["target"]) for porta in portas] == [("127.0.0.1", 8000)]


def test_api_depende_de_postgres_e_rabbitmq_saudaveis(configuracao: dict[str, Any]) -> None:
    dependencias = configuracao["api"]["depends_on"]
    assert {nome: d["condition"] for nome, d in dependencias.items()} == {
        "postgres": "service_healthy",
        "rabbitmq": "service_healthy",
    }


@pytest.mark.parametrize("servico", [*IMAGENS, *CONSTRUIDOS])
def test_restart_e_health_check(configuracao: dict[str, Any], servico: str) -> None:
    assert configuracao[servico]["restart"] == "unless-stopped"
    assert configuracao[servico]["healthcheck"]["test"]


@pytest.mark.parametrize("servico", IMAGENS)
def test_portas_publicadas_so_em_loopback(configuracao: dict[str, Any], servico: str) -> None:
    portas = {
        (porta.get("host_ip"), porta["published"])
        for porta in configuracao[servico].get("ports", [])
    }
    assert portas == PORTAS[servico]


@pytest.mark.parametrize("servico", [*IMAGENS, *CONSTRUIDOS])
def test_servico_saudavel(servico: str) -> None:
    assert _servicos_em_execucao()[servico]["Health"] == "healthy"
