import ast
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
SERVICOS_SEM_BANCO = ["agent", "mcp_clinica", "painel"]
# O DatabaseSessionService do ADK precisa do driver (T3.3), com um usuário que só vê as próprias
# tabelas: ver tests/test_sessoes_adk.py. O código do agente continua sem importar banco.
DRIVER_PERMITIDO = {"agent": ("asyncpg",)}
PROIBIDOS = ("clinica_core.db", "clinica_core.modelos", "sqlalchemy", "asyncpg")


def _imports_proibidos(codigo: str) -> set[str]:
    encontrados: set[str] = set()
    for no in ast.walk(ast.parse(codigo)):
        if isinstance(no, ast.Import):
            nomes = [apelido.name for apelido in no.names]
        elif isinstance(no, ast.ImportFrom) and no.module is not None:
            nomes = [no.module, *(f"{no.module}.{apelido.name}" for apelido in no.names)]
        else:
            continue
        for nome in nomes:
            if any(nome == p or nome.startswith(f"{p}.") for p in PROIBIDOS):
                encontrados.add(nome)
    return encontrados


@pytest.mark.parametrize(
    "codigo",
    [
        "import sqlalchemy.ext.asyncio",
        "import asyncpg",
        "from clinica_core.db import criar_engine",
        "from clinica_core import db",
        "from clinica_core import modelos",
    ],
)
def test_detector_pega_import_proibido(codigo: str) -> None:
    assert _imports_proibidos(codigo)


def test_detector_aceita_mensageria() -> None:
    assert not _imports_proibidos("from clinica_core.mensageria import publicar")


@pytest.mark.parametrize("servico", SERVICOS_SEM_BANCO)
def test_servico_nao_importa_banco(servico: str) -> None:
    pasta = RAIZ / "services" / servico
    # Os testes do painel sobem a API de verdade, e precisam preparar os dados dela.
    arquivos = [a for a in pasta.rglob("*.py") if "tests" not in a.relative_to(pasta).parts]
    assert arquivos
    for arquivo in arquivos:
        assert not _imports_proibidos(arquivo.read_text()), arquivo


@pytest.mark.parametrize("servico", SERVICOS_SEM_BANCO)
def test_servico_nao_declara_dependencia_de_banco(servico: str) -> None:
    projeto = tomllib.loads((RAIZ / "services" / servico / "pyproject.toml").read_text())
    for dependencia in projeto["project"]["dependencies"]:
        if dependencia in DRIVER_PERMITIDO.get(servico, ()):
            continue
        assert not dependencia.startswith(("sqlalchemy", "asyncpg")), dependencia
        if dependencia.startswith("clinica-core["):
            assert "db" not in dependencia.split("[")[1], dependencia


def test_ler_eventos_nao_carrega_o_banco() -> None:
    # O painel e o agente não têm o sqlalchemy na imagem, e um import indireto só apareceria nela.
    codigo = (
        "import sys, clinica_core.eventos, clinica_core.mensageria, clinica_core.config;"
        "sys.exit(any(m in sys.modules for m in ('sqlalchemy', 'pgvector')))"
    )
    assert subprocess.run([sys.executable, "-c", codigo], check=False).returncode == 0
