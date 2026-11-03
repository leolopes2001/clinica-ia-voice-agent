import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

RAIZ = Path(__file__).resolve().parent.parent
SCRIPT = RAIZ / "infra" / "backup" / "backup.sh"


def _compose(*args: str) -> str:
    resultado = subprocess.run(
        ["docker", "compose", *args], cwd=RAIZ, capture_output=True, text=True, check=True
    )
    return resultado.stdout


@pytest.fixture(scope="module")
def stack_no_ar() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker não instalado")
    try:
        rodando = _compose("ps", "--status", "running", "--services")
    except subprocess.CalledProcessError:
        pytest.skip("daemon do docker indisponível")
    if "backup" not in rodando.split():
        pytest.skip("serviço backup parado: rode `docker compose up -d --wait`")


def test_script_passa_na_checagem_de_sintaxe() -> None:
    subprocess.run(["sh", "-n", str(SCRIPT)], check=True)


def test_copia_externa_so_sai_criptografada() -> None:
    texto = SCRIPT.read_text()
    assert "openssl enc -aes-256-cbc -pbkdf2" in texto
    # A senha nunca aparece na linha de comando, visível em `ps`.
    assert "-pass env:BACKUP_SENHA" in texto
    assert 'cp "$dump"' not in texto


@pytest.mark.usefixtures("stack_no_ar")
def test_backup_e_restauracao_de_verdade() -> None:
    _compose("exec", "-T", "backup", "sh", "/backup/backup.sh", "--uma-vez")
    estado = json.loads(_compose("exec", "-T", "backup", "cat", "/backups/ultimo.json"))
    assert estado["situacao"] == "ok"
    assert estado["tamanho"] > 0
    assert estado["arquivo"].startswith("clinica-")

    permissoes = _compose(
        "exec", "-T", "backup", "stat", "-c", "%a", f"/backups/{estado['arquivo']}"
    )
    assert permissoes.strip() == "600"

    banco = f"restaurado_{uuid4().hex}"
    try:
        saida = subprocess.run(
            [str(RAIZ / "scripts" / "restaurar_backup.sh"), estado["arquivo"], banco],
            cwd=RAIZ,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "clinicas: " in saida
        assert f"Restaurado em {banco}" in saida
        tabelas = _compose(
            "exec",
            "-T",
            "backup",
            "psql",
            "-d",
            banco,
            "-Atc",
            "select count(*) from pg_constraint where conname = 'sem_conflito_dentista'",
        )
        # A garantia da agenda precisa voltar junto com os dados.
        assert tabelas.strip() == "1"
    finally:
        _compose("exec", "-T", "backup", "dropdb", "--if-exists", "--force", banco)
