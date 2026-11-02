"""Conversa automática com o agente, com voz sintetizada pelo `say` do macOS, e mede a latência.

    uv run python scripts/conversa_voz.py --telefone +5511900000001 "Sim, pode." "Quanto custa..."

A latência vai do fim da fala do paciente até o primeiro som do agente. Turnos que chamam
ferramentas ficam mais lentos, e isso também é tempo de espera real na ligação.
"""

import argparse
import asyncio
import json
import statistics
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from livekit import rtc

from clinica_agent.config import ConfigLiveKit
from clinica_agent.despacho import despachar_chamada_de_teste

TAXA = 16000
AMOSTRAS_POR_QUADRO = TAXA // 50
LIMIAR_DE_SOM = 300
SILENCIO_DE_FIM_DE_TURNO_S = 2.0
ESPERA_MAXIMA_S = 45.0


@dataclass
class Estado:
    ultimo_som: float = 0.0
    primeiro_som_depois_da_fala: float | None = None
    fim_da_fala: float | None = None
    duracao_da_resposta: float = 0.0
    latencias: list[float] = field(default_factory=list)
    terminou_de_falar: asyncio.Event = field(default_factory=asyncio.Event)
    ferramentas: list[str] = field(default_factory=list)
    saiu: asyncio.Event = field(default_factory=asyncio.Event)


def _sintetizar(texto: str, voz: str) -> np.ndarray:
    with tempfile.TemporaryDirectory() as pasta:
        arquivo = Path(pasta) / "fala.wav"
        subprocess.run(
            [
                "say",
                "-v",
                voz,
                "-o",
                str(arquivo),
                "--file-format=WAVE",
                "--data-format=LEI16@16000",
            ]
            + [texto],
            check=True,
        )
        with wave.open(str(arquivo)) as audio:
            return np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16)


async def _ouvir(faixa: rtc.Track, estado: Estado) -> None:
    async for evento in rtc.AudioStream(faixa, sample_rate=TAXA, num_channels=1):
        amostras = np.frombuffer(bytes(evento.frame.data), dtype=np.int16)
        if amostras.size and np.sqrt(np.mean(amostras.astype(np.float64) ** 2)) > LIMIAR_DE_SOM:
            agora = time.monotonic()
            estado.ultimo_som = agora
            if estado.primeiro_som_depois_da_fala is not None:
                estado.duracao_da_resposta = agora - estado.primeiro_som_depois_da_fala
            if estado.fim_da_fala is not None and estado.primeiro_som_depois_da_fala is None:
                estado.primeiro_som_depois_da_fala = agora
                latencia = agora - estado.fim_da_fala
                estado.latencias.append(latencia)
                print(f"    [latência {latencia:.2f} s]", flush=True)


async def _microfone(
    fonte: rtc.AudioSource, fala: asyncio.Queue[np.ndarray], estado: Estado
) -> None:
    silencio = np.zeros(AMOSTRAS_POR_QUADRO, dtype=np.int16)
    while True:
        amostras = None if fala.empty() else fala.get_nowait()
        if amostras is None:
            await fonte.capture_frame(_quadro(silencio))
            continue
        for inicio in range(0, amostras.size, AMOSTRAS_POR_QUADRO):
            pedaco = amostras[inicio : inicio + AMOSTRAS_POR_QUADRO]
            if pedaco.size < AMOSTRAS_POR_QUADRO:
                pedaco = np.pad(pedaco, (0, AMOSTRAS_POR_QUADRO - pedaco.size))
            await fonte.capture_frame(_quadro(pedaco))
        estado.fim_da_fala = time.monotonic()
        estado.primeiro_som_depois_da_fala = None
        estado.terminou_de_falar.set()


def _quadro(amostras: np.ndarray) -> rtc.AudioFrame:
    return rtc.AudioFrame(amostras.tobytes(), TAXA, 1, amostras.size)


async def _esperar_fim_do_turno(estado: Estado) -> None:
    inicio = time.monotonic()
    while time.monotonic() - inicio < ESPERA_MAXIMA_S and not estado.saiu.is_set():
        await asyncio.sleep(0.1)
        respondeu = estado.ultimo_som > (estado.fim_da_fala or 0.0)
        if respondeu and time.monotonic() - estado.ultimo_som > SILENCIO_DE_FIM_DE_TURNO_S:
            return


async def conversar(telefone: str, falas: list[str], voz: str) -> list[float]:
    estado = Estado()
    config = ConfigLiveKit()
    _, token = await despachar_chamada_de_teste(config, telefone)
    sala = rtc.Room()
    tarefas: set[asyncio.Task[None]] = set()

    def faixa_assinada(faixa: rtc.Track, *_: object) -> None:
        if faixa.kind == rtc.TrackKind.KIND_AUDIO:
            tarefas.add(asyncio.create_task(_ouvir(faixa, estado)))

    def dados(pacote: rtc.DataPacket) -> None:
        if pacote.topic != "adk":
            return
        mensagem = json.loads(pacote.data)
        if mensagem["type"] == "function_call":
            estado.ferramentas.append(mensagem["name"])
            print(f"    -> {mensagem['name']}({json.dumps(mensagem['args'], ensure_ascii=False)})")
        else:
            resposta = json.dumps(mensagem["response"], ensure_ascii=False)
            print(f"    <- {mensagem['name']}: {resposta[:400]}", flush=True)

    def transcricao(leitor: rtc.TextStreamReader, participante: str) -> None:
        async def ler() -> None:
            atributos = leitor.info.attributes or {}
            texto = await leitor.read_all()
            if atributos.get("lk.transcription_final") == "true" and texto.strip():
                print(f"[{participante}] {texto.strip()}", flush=True)

        tarefas.add(asyncio.create_task(ler()))

    sala.on("track_subscribed", faixa_assinada)
    sala.on("data_received", dados)
    sala.on("disconnected", lambda *_: estado.saiu.set())
    sala.on("participant_disconnected", lambda *_: estado.saiu.set())
    sala.register_text_stream_handler("lk.transcription", transcricao)
    await sala.connect(config.url, token)

    fonte = rtc.AudioSource(TAXA, 1, queue_size_ms=100)
    faixa = rtc.LocalAudioTrack.create_audio_track("microfone", fonte)
    await sala.local_participant.publish_track(
        faixa, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    fala: asyncio.Queue[np.ndarray] = asyncio.Queue()
    tarefas.add(asyncio.create_task(_microfone(fonte, fala, estado)))

    estado.fim_da_fala = time.monotonic()
    await _esperar_fim_do_turno(estado)
    print(f"[abertura em {estado.latencias[0] if estado.latencias else -1:.2f} s]", flush=True)
    for texto in falas:
        if estado.saiu.is_set():
            break
        print(f"[paciente] {texto}", flush=True)
        amostras = await asyncio.to_thread(_sintetizar, texto, voz)
        estado.ultimo_som = 0.0
        estado.terminou_de_falar.clear()
        fala.put_nowait(amostras)
        await estado.terminou_de_falar.wait()
        await _esperar_fim_do_turno(estado)
    await asyncio.sleep(1.0)
    print(f"[última resposta: {estado.duracao_da_resposta:.1f} s de áudio]", flush=True)
    await sala.disconnect()
    for tarefa in tarefas:
        tarefa.cancel()
    return estado.latencias[1:]


def _principal() -> None:
    argumentos = argparse.ArgumentParser(description=__doc__)
    argumentos.add_argument("--telefone", default="+5511900000001")
    argumentos.add_argument("--voz", default="Luciana")
    argumentos.add_argument("falas", nargs="*")
    lidos = argumentos.parse_args()
    latencias = asyncio.run(conversar(lidos.telefone, lidos.falas, lidos.voz))
    if latencias:
        ordenadas = sorted(latencias)
        p95 = ordenadas[max(0, round(0.95 * len(ordenadas)) - 1)]
        mediana = statistics.median(ordenadas)
        print(f"turnos={len(ordenadas)} mediana={mediana:.2f}s p95={p95:.2f}s", flush=True)


if __name__ == "__main__":
    _principal()
