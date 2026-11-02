"""Trava abaixo do modelo: executar só passa com confirmação explícita na fala do paciente."""

import asyncio
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

from google.adk.events import Event
from google.adk.tools.base_tool import BaseTool

from clinica_agent.resumo import resultado_da_ferramenta

logger = logging.getLogger(__name__)

_PREPARO_DA_EXECUCAO = {
    "executar_cancelamento": "preparar_cancelamento",
    "executar_remarcacao": "preparar_remarcacao",
}
_RECUSAS_DA_TRAVA = ("confirmacao_ausente", "preparo_ausente")
_CAMPOS_DA_EXECUCAO = ("agendamento_id", "hold_id")
# O Gemini 3.x entrega a chamada de ferramenta sem esperar a transcrição da fala.
ESPERA_PELA_TRANSCRICAO_S = 2.0
# A fala pode ter mais trechos a caminho: "sim", e depois "mas espera".
ASSENTAR_TRANSCRICAO_S = 0.3
_CONFIRMA = re.compile(r"\b(sim|confirmo|pode (confirmar|cancelar|remarcar|trocar))\b")
_DESFAZ = re.compile(r"\b(nao|pode ser|talvez|acho|espera|espere|perai|mas|depende|sei la)\b")


def _normalizar(texto: str) -> str:
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn"
    )
    return " ".join(re.sub(r"[^\w\s]", " ", sem_acento.lower()).split())


def confirmacao_explicita(fala: str) -> bool:
    if "?" in fala:
        return False
    normalizada = _normalizar(fala)
    return bool(_CONFIRMA.search(normalizada)) and not _DESFAZ.search(normalizada)


@dataclass(frozen=True)
class _Fala:
    texto: str
    inicio: float


@dataclass(frozen=True)
class _Preparo:
    ferramenta: str
    argumentos: dict[str, str]
    concluido_em: float


def _argumentos_da_acao(args: dict[str, Any]) -> dict[str, str]:
    return {campo: str(args[campo]) for campo in _CAMPOS_DA_EXECUCAO if campo in args}


class TravaDeConfirmacao:
    """Uma por ligação. Guarda só a última fala e o último preparo, na memória do processo."""

    def __init__(
        self,
        espera_s: float = ESPERA_PELA_TRANSCRICAO_S,
        assentar_s: float = ASSENTAR_TRANSCRICAO_S,
    ) -> None:
        self._espera_s = espera_s
        self._assentar_s = assentar_s
        self._parcial = ""
        self._inicio_do_parcial = 0.0
        self._ultima_final: _Fala | None = None
        self._preparo: _Preparo | None = None
        self._mudou = asyncio.Event()

    def observar(self, evento: Event) -> None:
        transcricao = evento.input_transcription
        if transcricao is None or not transcricao.text:
            return
        agora = time.monotonic()
        if not self._parcial:
            self._inicio_do_parcial = agora
        if transcricao.finished:
            self._ultima_final = _Fala(transcricao.text, self._inicio_do_parcial)
            self._parcial = ""
        else:
            self._parcial += transcricao.text
        self._mudou.set()

    def _ultima_fala(self) -> _Fala | None:
        if self._parcial:
            return _Fala(self._parcial, self._inicio_do_parcial)
        return self._ultima_final

    def _confirmou_depois_do_preparo(self, preparo: _Preparo) -> bool:
        fala = self._ultima_fala()
        return (
            fala is not None
            and fala.inicio > preparo.concluido_em
            and confirmacao_explicita(fala.texto)
        )

    async def _esperar_mudanca(self, segundos: float) -> bool:
        self._mudou.clear()
        try:
            async with asyncio.timeout(segundos):
                await self._mudou.wait()
        except TimeoutError:
            return False
        return True

    async def _paciente_confirmou(self, preparo: _Preparo) -> bool:
        limite = time.monotonic() + self._espera_s
        while not self._confirmou_depois_do_preparo(preparo):
            restante = limite - time.monotonic()
            if restante <= 0 or not await self._esperar_mudanca(restante):
                return False
        while await self._esperar_mudanca(self._assentar_s):
            pass
        return self._confirmou_depois_do_preparo(preparo)

    async def antes_da_ferramenta(
        self, tool: BaseTool, args: dict[str, Any], tool_context: Any
    ) -> dict[str, Any] | None:
        preparo_exigido = _PREPARO_DA_EXECUCAO.get(tool.name)
        if preparo_exigido is None:
            return None
        preparo = self._preparo
        if (
            preparo is None
            or preparo.ferramenta != preparo_exigido
            or preparo.argumentos != _argumentos_da_acao(args)
        ):
            logger.warning("%s bloqueada: preparo_ausente", tool.name)
            return {"ok": False, "motivo": "preparo_ausente"}
        if not await self._paciente_confirmou(preparo):
            # Sem o texto da fala no log: pode ter dado de saúde.
            logger.warning("%s bloqueada: confirmacao_ausente", tool.name)
            return {"ok": False, "motivo": "confirmacao_ausente"}
        return None

    async def depois_da_ferramenta(
        self, tool: BaseTool, args: dict[str, Any], tool_context: Any, tool_response: Any
    ) -> None:
        if tool.name in _PREPARO_DA_EXECUCAO:
            # O ADK também passa aqui com a recusa da trava, e essa não gasta o preparo: o paciente
            # ainda pode dizer "sim". Fora isso, cada preparo vale para uma execução.
            motivo = resultado_da_ferramenta(tool_response).get("motivo")
            if motivo not in _RECUSAS_DA_TRAVA:
                self._preparo = None
        elif (
            tool.name in _PREPARO_DA_EXECUCAO.values()
            and resultado_da_ferramenta(tool_response).get("ok") is True
        ):
            self._preparo = _Preparo(tool.name, _argumentos_da_acao(args), time.monotonic())
