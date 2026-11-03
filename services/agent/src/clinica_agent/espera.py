"""Frase de espera tocada pelo próprio agente: pedir ao modelo não adiantou (T3.7)."""

import asyncio
import contextlib
import logging
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from google.adk.events import Event
from google.adk.tools.base_tool import BaseTool
from livekit import rtc

logger = logging.getLogger(__name__)

# Quando o paciente pergunta e espera uma informação. Decidido pelo usuário em 02/11/2026.
FERRAMENTAS_COM_ESPERA = frozenset(
    {"buscar_conhecimento", "listar_horarios_livres", "listar_meus_agendamentos"}
)
INTERVALO_MINIMO_S = 10.0
# Se o modelo acabou de falar, a pessoa não está em silêncio, e a frase ficaria por cima.
SILENCIO_MINIMO_DO_MODELO_S = 2.0
_QUADRO_MS = 20
_BYTES_POR_AMOSTRA = 2


@dataclass(frozen=True)
class AudioDaFrase:
    amostras: bytes
    taxa: int


def carregar_audio(caminho: Path) -> AudioDaFrase | None:
    """Sem o arquivo, ou com outro formato, a ligação segue sem a frase."""
    try:
        with wave.open(str(caminho), "rb") as arquivo:
            if arquivo.getnchannels() != 1 or arquivo.getsampwidth() != _BYTES_POR_AMOSTRA:
                logger.warning("Frase de espera ignorada: o áudio precisa ser mono e de 16 bits")
                return None
            return AudioDaFrase(arquivo.readframes(arquivo.getnframes()), arquivo.getframerate())
    except (OSError, EOFError, wave.Error) as erro:
        logger.info("Ligação sem frase de espera: %r", erro)
        return None


class FonteDeAudio(Protocol):
    async def capture_frame(self, frame: rtc.AudioFrame) -> None: ...

    def clear_queue(self) -> None: ...


def _modelo_falou(evento: Event) -> bool:
    if evento.output_transcription and evento.output_transcription.text:
        return True
    partes = evento.content.parts if evento.content and evento.content.parts else []
    return any(
        parte.inline_data is not None
        and (parte.inline_data.mime_type or "").startswith("audio/")
        and evento.author != "user"
        for parte in partes
    )


class FraseDeEspera:
    """Uma por ligação. Nunca atrapalha a ferramenta: toca em paralelo, e uma falha só vira log."""

    def __init__(
        self,
        audio: AudioDaFrase,
        fonte: FonteDeAudio,
        intervalo_minimo_s: float = INTERVALO_MINIMO_S,
        silencio_minimo_s: float = SILENCIO_MINIMO_DO_MODELO_S,
    ) -> None:
        self._audio = audio
        self._fonte = fonte
        self._intervalo_minimo_s = intervalo_minimo_s
        self._silencio_minimo_s = silencio_minimo_s
        self._tocou_em: float | None = None
        self._modelo_falou_em: float | None = None
        self._tarefa: asyncio.Task[None] | None = None
        self._fala_anterior_fechada = False

    @property
    def tocando(self) -> bool:
        tarefa = self._tarefa
        return tarefa is not None and not tarefa.done() and not tarefa.cancelling()

    async def antes_da_ferramenta(
        self, tool: BaseTool, args: dict[str, Any], tool_context: Any
    ) -> None:
        if tool.name not in FERRAMENTAS_COM_ESPERA or self.tocando:
            return
        agora = time.monotonic()
        if self._tocou_em is not None and agora - self._tocou_em < self._intervalo_minimo_s:
            return
        if (
            self._modelo_falou_em is not None
            and agora - self._modelo_falou_em < self._silencio_minimo_s
        ):
            return
        self._tocou_em = agora
        self._fala_anterior_fechada = False
        self._tarefa = asyncio.create_task(self._tocar())

    def observar(self, evento: Event) -> None:
        if _modelo_falou(evento):
            self._modelo_falou_em = time.monotonic()
            self.parar()
            return
        transcricao = evento.input_transcription
        if not self.tocando or transcricao is None or not transcricao.text:
            return
        # O Gemini 3.x entrega a ferramenta antes da transcrição da pergunta que a causou. Só depois
        # de ela fechar é que um novo trecho significa que a pessoa voltou a falar.
        if self._fala_anterior_fechada:
            self.parar()
        elif transcricao.finished:
            self._fala_anterior_fechada = True

    def parar(self) -> None:
        if self.tocando:
            assert self._tarefa is not None
            self._tarefa.cancel()
            self._fonte.clear_queue()

    async def fechar(self) -> None:
        self.parar()
        if self._tarefa is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._tarefa

    async def _tocar(self) -> None:
        amostras_por_quadro = self._audio.taxa * _QUADRO_MS // 1000
        tamanho = amostras_por_quadro * _BYTES_POR_AMOSTRA
        try:
            for inicio in range(0, len(self._audio.amostras), tamanho):
                pedaco = self._audio.amostras[inicio : inicio + tamanho]
                await self._fonte.capture_frame(
                    rtc.AudioFrame(pedaco, self._audio.taxa, 1, len(pedaco) // _BYTES_POR_AMOSTRA)
                )
        except Exception as erro:
            logger.warning("Falha ao tocar a frase de espera: %r", erro)


async def publicar_frase_de_espera(sala: rtc.Room, caminho: Path) -> FraseDeEspera | None:
    audio = carregar_audio(caminho)
    if audio is None:
        return None
    try:
        # Faixa própria: a do LiveKitRunner é dele, e quem liga ouve as duas misturadas.
        fonte = rtc.AudioSource(audio.taxa, 1)
        faixa = rtc.LocalAudioTrack.create_audio_track("frase-de-espera", fonte)
        await sala.local_participant.publish_track(
            faixa, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
    except Exception as erro:
        logger.warning("Ligação sem frase de espera: %r", erro)
        return None
    return FraseDeEspera(audio, fonte)
