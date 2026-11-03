"""Gera segredos fortes para a produção e os grava no `.env`, sem trocar os que já existem.

`uv run python scripts/gerar_segredos.py` e, depois, siga "Trocar as senhas" em docs/runbook.md:
as senhas do Postgres e do usuário das sessões já estão no banco, e mudam só por ALTER ROLE.
"""

import argparse
import os
import secrets
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SEGREDOS = (
    "POSTGRES_PASSWORD",
    "RABBITMQ_PASSWORD",
    "ADK_SESSOES_SENHA",
    "LIVEKIT_API_SECRET",
    "API_TOKEN_PAINEL",
    "API_TOKEN_SERVICOS",
    "PAINEL_SEGREDO_SESSAO",
    "BACKUP_SENHA",
)
# 32 bytes aleatórios.
_TAMANHO = 32


def _definidos(texto: str) -> set[str]:
    definidos = set()
    for linha in texto.splitlines():
        nome, separador, valor = linha.partition("=")
        if separador and valor.strip() and not nome.lstrip().startswith("#"):
            definidos.add(nome.strip())
    return definidos


def gerar(arquivo: Path) -> list[str]:
    """Devolve os nomes gerados, e nunca os valores: isto pode cair num log de terminal."""
    existente = arquivo.read_text(encoding="utf-8") if arquivo.exists() else ""
    definidos = _definidos(existente)
    novos = [nome for nome in SEGREDOS if nome not in definidos]
    if not novos:
        return []
    # URL-safe: estes valores entram em URLs de conexão e no Compose.
    linhas = [f"{nome}={secrets.token_urlsafe(_TAMANHO)}" for nome in novos]
    # Tira as linhas vazias, como `BACKUP_SENHA=`: a última definição é a que vale, mas confunde.
    mantidas = [
        linha for linha in existente.splitlines() if linha.partition("=")[0].strip() not in novos
    ]
    conteudo = "\n".join([*mantidas, *linhas]) + "\n"
    descritor = os.open(arquivo, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descritor, "w", encoding="utf-8") as saida:
        saida.write(conteudo)
    arquivo.chmod(0o600)
    return novos


if __name__ == "__main__":
    argumentos = argparse.ArgumentParser(description=__doc__)
    argumentos.add_argument("--arquivo", type=Path, default=RAIZ / ".env")
    gerados = gerar(argumentos.parse_args().arquivo)
    if gerados:
        print("Gerados no arquivo, sem mostrar os valores:")
        print("\n".join(f"  {nome}" for nome in gerados))
    else:
        print("Todos os segredos já estavam definidos. Nada mudou.")
