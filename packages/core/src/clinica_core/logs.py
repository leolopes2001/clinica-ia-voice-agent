"""Logs em JSON, uma linha por evento, para qualquer ferramenta ler."""

import json
import logging
import sys
from datetime import UTC, datetime

# Estes registram URLs, com a busca por nome de paciente, ou o conteúdo da conversa (LGPD).
_SO_AVISOS = ("httpx", "httpcore", "google_adk", "google_genai", "mcp")
_DO_UVICORN = ("uvicorn", "uvicorn.error", "uvicorn.access")


class FormatoJson(logging.Formatter):
    def __init__(self, servico: str) -> None:
        super().__init__()
        self._servico = servico

    def format(self, record: logging.LogRecord) -> str:
        linha = {
            "quando": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "nivel": record.levelname,
            "servico": self._servico,
            "logger": record.name,
            "mensagem": record.getMessage(),
        }
        if record.exc_info:
            linha["erro"] = self.formatException(record.exc_info)
        return json.dumps(linha, ensure_ascii=False)


def configurar_logs(servico: str, nivel: int = logging.INFO) -> None:
    saida = logging.StreamHandler(sys.stdout)
    saida.setFormatter(FormatoJson(servico))
    raiz = logging.getLogger()
    raiz.handlers = [saida]
    raiz.setLevel(nivel)
    for nome in _DO_UVICORN:
        logger = logging.getLogger(nome)
        logger.handlers = []
        logger.propagate = True
    # O log de acesso traz a URL de cada pedido, e não há como tirar só a busca por paciente.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for nome in _SO_AVISOS:
        logging.getLogger(nome).setLevel(logging.WARNING)
