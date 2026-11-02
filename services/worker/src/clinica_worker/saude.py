import sys
import time
from pathlib import Path

from clinica_worker.principal import ARQUIVO_VIVO, INTERVALO_VARREDURA_S

_LIMITE_S = 3 * INTERVALO_VARREDURA_S


def saudavel(arquivo: Path = ARQUIVO_VIVO, limite_s: float = _LIMITE_S) -> bool:
    try:
        return time.time() - arquivo.stat().st_mtime <= limite_s
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(0 if saudavel() else 1)
