import importlib.util
import stat
from pathlib import Path
from types import ModuleType

import pytest

RAIZ = Path(__file__).resolve().parent.parent


@pytest.fixture
def modulo() -> ModuleType:
    especificacao = importlib.util.spec_from_file_location(
        "gerar_segredos", RAIZ / "scripts" / "gerar_segredos.py"
    )
    assert especificacao is not None
    assert especificacao.loader is not None
    carregado = importlib.util.module_from_spec(especificacao)
    especificacao.loader.exec_module(carregado)
    return carregado


def _valores(arquivo: Path) -> dict[str, str]:
    linhas = arquivo.read_text(encoding="utf-8").splitlines()
    return {nome: valor for nome, _, valor in (linha.partition("=") for linha in linhas) if valor}


def test_gera_todos_com_permissao_so_do_dono(modulo: ModuleType, tmp_path: Path) -> None:
    arquivo = tmp_path / ".env"
    assert modulo.gerar(arquivo) == list(modulo.SEGREDOS)
    valores = _valores(arquivo)
    assert set(valores) == set(modulo.SEGREDOS)
    assert all(len(valor) >= 40 for valor in valores.values())
    assert len(set(valores.values())) == len(modulo.SEGREDOS)
    assert stat.S_IMODE(arquivo.stat().st_mode) == 0o600


def test_nao_troca_o_que_ja_existe_e_mantem_o_resto(modulo: ModuleType, tmp_path: Path) -> None:
    arquivo = tmp_path / ".env"
    arquivo.write_text(
        "GOOGLE_API_KEY=minha-chave\nPOSTGRES_PASSWORD=ja-trocada\n# comentário\nBACKUP_SENHA=\n",
        encoding="utf-8",
    )
    gerados = modulo.gerar(arquivo)
    assert "POSTGRES_PASSWORD" not in gerados
    assert "BACKUP_SENHA" in gerados
    valores = _valores(arquivo)
    assert valores["GOOGLE_API_KEY"] == "minha-chave"
    assert valores["POSTGRES_PASSWORD"] == "ja-trocada"
    assert valores["BACKUP_SENHA"]
    assert "# comentário" in arquivo.read_text(encoding="utf-8")


def test_rodar_de_novo_nao_muda_nada(modulo: ModuleType, tmp_path: Path) -> None:
    arquivo = tmp_path / ".env"
    modulo.gerar(arquivo)
    antes = arquivo.read_text(encoding="utf-8")
    assert modulo.gerar(arquivo) == []
    assert arquivo.read_text(encoding="utf-8") == antes


def test_nada_de_valor_e_impresso(
    modulo: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arquivo = tmp_path / ".env"
    modulo.gerar(arquivo)
    assert capsys.readouterr().out == ""


def test_cobre_as_senhas_padrao_do_compose(modulo: ModuleType) -> None:
    compose = (RAIZ / "docker-compose.yml").read_text(encoding="utf-8")
    for nome in modulo.SEGREDOS:
        assert f"${{{nome}" in compose, nome
