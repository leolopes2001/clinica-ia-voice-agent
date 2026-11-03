import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

RAIZ = Path(__file__).resolve().parent.parent
IMAGENS = {
    "backup": "pgvector/pgvector:pg16",
    "postgres": "pgvector/pgvector:pg16",
    "rabbitmq": "rabbitmq:3-management",
    "redis": "redis:7",
    "livekit": "livekit/livekit-server:v1.13.11",
    "livekit-sip": "livekit/sip:v1.22.0",
}
CONSTRUIDOS = ["api", "painel", "worker", "mcp-clinica", "mcp-conhecimento", "agent"]
DOCKERFILES = {servico: servico.replace("-", "_") for servico in CONSTRUIDOS}
PORTAS = {
    "backup": set[tuple[str, str]](),
    "postgres": {("127.0.0.1", "5432")},
    "rabbitmq": {("127.0.0.1", "5672"), ("127.0.0.1", "15672")},
    "redis": set[tuple[str, str]](),
    "livekit": {
        ("127.0.0.1", "7880"),
        ("127.0.0.1", "7881"),
        ("127.0.0.1", "7882"),
        ("127.0.0.1", "5060"),
        *(("127.0.0.1", str(porta)) for porta in range(10000, 10101)),
    },
    "livekit-sip": set[tuple[str, str]](),
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
    assert (
        configuracao[servico]["build"]["dockerfile"]
        == f"services/{DOCKERFILES[servico]}/Dockerfile"
    )


def test_api_publicada_so_em_loopback(configuracao: dict[str, Any]) -> None:
    portas = configuracao["api"]["ports"]
    assert [(porta.get("host_ip"), porta["target"]) for porta in portas] == [("127.0.0.1", 8000)]


def test_api_depende_de_postgres_e_rabbitmq_saudaveis(configuracao: dict[str, Any]) -> None:
    dependencias = configuracao["api"]["depends_on"]
    assert {nome: d["condition"] for nome, d in dependencias.items()} == {
        "postgres": "service_healthy",
        "rabbitmq": "service_healthy",
    }


def test_agente_sem_portas_e_na_rede_do_livekit(configuracao: dict[str, Any]) -> None:
    agente = configuracao["agent"]
    assert "ports" not in agente
    assert agente["network_mode"] == "service:livekit"
    assert agente["depends_on"]["mcp-clinica"]["condition"] == "service_healthy"
    assert agente["depends_on"]["mcp-conhecimento"]["condition"] == "service_healthy"
    assert "AGENTE_MODELO_VOZ" in agente["environment"]


def test_sip_na_rede_do_livekit_com_padrao_so_na_propria_maquina(
    configuracao: dict[str, Any],
) -> None:
    sip = configuracao["livekit-sip"]
    assert sip["network_mode"] == "service:livekit"
    assert "ports" not in sip
    corpo = sip["environment"]["SIP_CONFIG_BODY"]
    assert "hide_inbound_port: true" in corpo
    assert "rtp_port: 10000-10100" in corpo


def test_telefone_de_teste_vem_desligado_por_padrao(
    configuracao: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        "${AGENTE_PERMITIR_TELEFONE_DE_TESTE:-false}" in (RAIZ / "docker-compose.yml").read_text()
    )


def test_pagina_de_teste_so_com_perfil(configuracao: dict[str, Any]) -> None:
    assert "teste-navegador" not in configuracao


def test_painel_na_8080_so_em_loopback_por_padrao(configuracao: dict[str, Any]) -> None:
    portas = configuracao["painel"]["ports"]
    assert [(porta.get("host_ip"), porta["published"]) for porta in portas] == [
        ("127.0.0.1", "8080")
    ]


def test_painel_recebe_o_volume_da_base_e_nao_o_banco(configuracao: dict[str, Any]) -> None:
    painel = configuracao["painel"]
    assert [(v["source"], v["target"]) for v in painel["volumes"]] == [
        ("conhecimento_arquivos", "/dados/conhecimento"),
        ("backups", "/backups"),
    ]
    assert painel["volumes"][1]["read_only"] is True
    assert not [nome for nome in painel["environment"] if nome.startswith("POSTGRES_")]
    assert "postgres" not in painel["depends_on"]


def test_backup_le_a_base_sem_poder_alterar(configuracao: dict[str, Any]) -> None:
    volumes = {v["target"]: v for v in configuracao["backup"]["volumes"]}
    assert volumes["/conhecimento"]["read_only"] is True
    assert volumes["/backups"]["source"] == "backups"


def test_worker_nao_publica_portas(configuracao: dict[str, Any]) -> None:
    assert "ports" not in configuracao["worker"]


def test_mcp_clinica_so_na_rede_interna(configuracao: dict[str, Any]) -> None:
    assert "ports" not in configuracao["mcp-clinica"]
    assert configuracao["mcp-clinica"]["environment"]["MCP_CLINICA_API_URL"] == "http://api:8000"
    assert configuracao["mcp-clinica"]["depends_on"]["api"]["condition"] == "service_healthy"


def test_mcp_conhecimento_so_na_rede_interna(configuracao: dict[str, Any]) -> None:
    servico = configuracao["mcp-conhecimento"]
    assert "ports" not in servico
    assert "GOOGLE_API_KEY" in servico["environment"]
    assert servico["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_worker_recebe_a_chave_e_o_volume_da_base(configuracao: dict[str, Any]) -> None:
    worker = configuracao["worker"]
    assert "GOOGLE_API_KEY" in worker["environment"]
    assert worker["environment"]["CONHECIMENTO_DIR"] == "/dados/conhecimento"
    assert [(v["source"], v["target"]) for v in worker["volumes"]] == [
        ("conhecimento_arquivos", "/dados/conhecimento")
    ]


def test_worker_depende_de_api_postgres_e_rabbitmq_saudaveis(
    configuracao: dict[str, Any],
) -> None:
    dependencias = configuracao["worker"]["depends_on"]
    assert {nome: d["condition"] for nome, d in dependencias.items()} == {
        "api": "service_healthy",
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
