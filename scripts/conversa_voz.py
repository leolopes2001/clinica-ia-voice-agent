"""Conversa automática com o agente, com voz sintetizada pelo `say` do macOS, e mede a latência.

    uv run python scripts/conversa_voz.py --telefone +5511900000001 "Sim, pode." "Quanto custa..."

Com --interromper-apos N, fala por cima da abertura depois de N segundos (barge-in).

A latência vai do fim da fala do paciente até o primeiro som do agente. Em turnos com ferramenta,
esse som pode ser só a frase de espera, então "até a resposta" mede também o primeiro som depois
do resultado da ferramenta, depois de uma pausa. Essa é a espera real pela informação.
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
# Pausa que separa a frase de espera do começo da resposta, se ela ainda tocava ao vir o resultado.
PAUSA_ENTRE_FALAS_S = 0.2
ESPERA_MAXIMA_S = 45.0


@dataclass
class Estado:
    ultimo_som: float = 0.0
    primeiro_som_depois_da_fala: float | None = None
    fim_da_fala: float | None = None
    duracao_da_resposta: float = 0.0
    latencias: list[float] = field(default_factory=list)
    com_ferramenta: list[float] = field(default_factory=list)
    ate_a_resposta: list[float] = field(default_factory=list)
    resultado_em: float | None = None
    sem_ferramenta: list[float] = field(default_factory=list)
    chamou_ferramenta: bool = False
    pedidas: dict[str, float] = field(default_factory=dict)
    duracao_por_ferramenta: dict[str, list[float]] = field(default_factory=dict)
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
            pausa = agora - estado.ultimo_som
            estado.ultimo_som = agora
            if (
                estado.resultado_em is not None
                and estado.fim_da_fala is not None
                and pausa > PAUSA_ENTRE_FALAS_S
            ):
                estado.ate_a_resposta.append(agora - estado.fim_da_fala)
                estado.resultado_em = None
            if estado.primeiro_som_depois_da_fala is not None:
                estado.duracao_da_resposta = agora - estado.primeiro_som_depois_da_fala
            if estado.fim_da_fala is not None and estado.primeiro_som_depois_da_fala is None:
                estado.primeiro_som_depois_da_fala = agora
                latencia = agora - estado.fim_da_fala
                estado.latencias.append(latencia)
                grupo = estado.com_ferramenta if estado.chamou_ferramenta else estado.sem_ferramenta
                grupo.append(latencia)
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


def _resumo(rotulo: str, valores: list[float]) -> str:
    if not valores:
        return f"{rotulo}: nenhum"
    ordenados = sorted(valores)
    p95 = ordenados[max(0, round(0.95 * len(ordenados)) - 1)]
    mediana = statistics.median(ordenados)
    return f"{rotulo}: n={len(ordenados)} mediana={mediana:.2f}s p95={p95:.2f}s"


async def conversar(telefone: str, falas: list[str], voz: str, interromper_apos: float) -> Estado:
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
            estado.chamou_ferramenta = True
            estado.pedidas[mensagem["id"]] = time.monotonic()
            print(f"    -> {mensagem['name']}({json.dumps(mensagem['args'], ensure_ascii=False)})")
        else:
            estado.resultado_em = time.monotonic()
            pedida = estado.pedidas.pop(mensagem["id"], None)
            if pedida is not None:
                estado.duracao_por_ferramenta.setdefault(mensagem["name"], []).append(
                    time.monotonic() - pedida
                )
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
    if interromper_apos:
        await asyncio.sleep(interromper_apos)
        amostras = await asyncio.to_thread(_sintetizar, "Espera, espera um pouquinho.", voz)
        inicio_da_fala = time.monotonic()
        estado.terminou_de_falar.clear()
        fala.put_nowait(amostras)
        await asyncio.sleep(0.3)
        ouvindo_ate = time.monotonic() + 6.0
        ultimo_antes = 0.0
        while time.monotonic() < ouvindo_ate and not estado.terminou_de_falar.is_set():
            ultimo_antes = estado.ultimo_som
            await asyncio.sleep(0.05)
        print(f"[barge-in: agente parou {ultimo_antes - inicio_da_fala:.2f} s após a fala começar]")
    await _esperar_fim_do_turno(estado)
    print(f"[abertura em {estado.latencias[0] if estado.latencias else -1:.2f} s]", flush=True)
    for texto in falas:
        if estado.saiu.is_set():
            break
        print(f"[paciente] {texto}", flush=True)
        amostras = await asyncio.to_thread(_sintetizar, texto, voz)
        estado.ultimo_som = 0.0
        estado.chamou_ferramenta = False
        estado.terminou_de_falar.clear()
        fala.put_nowait(amostras)
        await estado.terminou_de_falar.wait()
        await _esperar_fim_do_turno(estado)
    await asyncio.sleep(1.0)
    print(f"[última resposta: {estado.duracao_da_resposta:.1f} s de áudio]", flush=True)
    await sala.disconnect()
    for tarefa in tarefas:
        tarefa.cancel()
    return estado


def _principal() -> None:
    argumentos = argparse.ArgumentParser(description=__doc__)
    argumentos.add_argument("--telefone", default="+5511900000001")
    argumentos.add_argument("--voz", default="Luciana")
    argumentos.add_argument("--interromper-apos", type=float, default=0.0)
    argumentos.add_argument("falas", nargs="*")
    lidos = argumentos.parse_args()
    estado = asyncio.run(conversar(lidos.telefone, lidos.falas, lidos.voz, lidos.interromper_apos))
    # A abertura fica de fora: ela não vem depois de uma fala do paciente.
    print(_resumo("todos os turnos", estado.latencias[1:]))
    print(_resumo("sem ferramenta", estado.sem_ferramenta[1:]))
    print(_resumo("com ferramenta, primeiro som", estado.com_ferramenta))
    print(_resumo("com ferramenta, até a resposta", estado.ate_a_resposta))
    for nome, duracoes in estado.duracao_por_ferramenta.items():
        print(_resumo(f"ferramenta {nome}", duracoes))


if __name__ == "__main__":
    _principal()
