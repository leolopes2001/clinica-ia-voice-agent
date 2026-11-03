import asyncio
import wave
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from google.adk.events import Event
from google.adk.tools.base_tool import BaseTool
from google.genai import types
from livekit import rtc

from clinica_agent.chamada import ChamadaAberta
from clinica_agent.config import ConfigAgente
from clinica_agent.espera import (
    FERRAMENTAS_COM_ESPERA,
    AudioDaFrase,
    FraseDeEspera,
    carregar_audio,
)
from clinica_agent.recepcionista import criar_agente
from clinica_agent.trava import TravaDeConfirmacao

TAXA = 8000
# 1 s de áudio: 50 quadros de 20 ms.
AUDIO = AudioDaFrase(b"\x01\x00" * TAXA, TAXA)


class _Ferramenta(BaseTool):
    def __init__(self, nome: str) -> None:
        super().__init__(name=nome, description=nome)


BUSCA = _Ferramenta("buscar_conhecimento")


class _Fonte:
    def __init__(self, espera_s: float = 0.0, erro: Exception | None = None) -> None:
        self.quadros: list[rtc.AudioFrame] = []
        self.limpezas = 0
        self._espera_s = espera_s
        self._erro = erro

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        if self._erro:
            raise self._erro
        self.quadros.append(frame)
        await asyncio.sleep(self._espera_s)

    def clear_queue(self) -> None:
        self.limpezas += 1


def _frase(fonte: _Fonte, **opcoes: float) -> FraseDeEspera:
    return FraseDeEspera(AUDIO, fonte, **opcoes)


async def _ate_o_fim(frase: FraseDeEspera) -> None:
    if frase._tarefa is not None:
        await frase._tarefa


def _fala_do_paciente(texto: str, finished: bool = False) -> Event:
    return Event(
        author="user",
        input_transcription=types.Transcription(text=texto, finished=finished),
        partial=not finished,
    )


def _fala_do_modelo() -> Event:
    return Event(author="recepcionista", output_transcription=types.Transcription(text="O valor"))


def _audio_do_modelo() -> Event:
    parte = types.Part(inline_data=types.Blob(data=b"\x00", mime_type="audio/pcm"))
    return Event(author="recepcionista", content=types.Content(role="model", parts=[parte]))


def test_ferramentas_escolhidas() -> None:
    assert FERRAMENTAS_COM_ESPERA == {
        "buscar_conhecimento",
        "listar_horarios_livres",
        "listar_meus_agendamentos",
    }


@pytest.mark.parametrize("nome", sorted(FERRAMENTAS_COM_ESPERA))
async def test_toca_o_audio_inteiro_em_quadros_de_20_ms(nome: str) -> None:
    fonte = _Fonte()
    frase = _frase(fonte)
    await frase.antes_da_ferramenta(_Ferramenta(nome), {}, None)
    await _ate_o_fim(frase)
    assert len(fonte.quadros) == 50
    assert {q.samples_per_channel for q in fonte.quadros} == {TAXA // 50}
    assert {(q.sample_rate, q.num_channels) for q in fonte.quadros} == {(TAXA, 1)}


@pytest.mark.parametrize(
    "nome", ["segurar_horario", "confirmar_agendamento", "registrar_consentimento", "end_call"]
)
async def test_outras_ferramentas_nao_tocam(nome: str) -> None:
    fonte = _Fonte()
    frase = _frase(fonte)
    await frase.antes_da_ferramenta(_Ferramenta(nome), {}, None)
    assert not frase.tocando
    assert fonte.quadros == []


async def test_nao_segura_a_ferramenta() -> None:
    frase = _frase(_Fonte(espera_s=10))
    await asyncio.wait_for(frase.antes_da_ferramenta(BUSCA, {}, None), timeout=0.1)
    assert frase.tocando
    await frase.fechar()


async def test_nao_repete_dentro_do_intervalo() -> None:
    fonte = _Fonte()
    frase = _frase(fonte)
    await frase.antes_da_ferramenta(BUSCA, {}, None)
    await _ate_o_fim(frase)
    await frase.antes_da_ferramenta(BUSCA, {}, None)
    assert not frase.tocando
    assert len(fonte.quadros) == 50


async def test_repete_depois_do_intervalo() -> None:
    fonte = _Fonte()
    frase = _frase(fonte, intervalo_minimo_s=0.001)
    await frase.antes_da_ferramenta(BUSCA, {}, None)
    await _ate_o_fim(frase)
    await asyncio.sleep(0.002)
    await frase.antes_da_ferramenta(BUSCA, {}, None)
    await _ate_o_fim(frase)
    assert len(fonte.quadros) == 100


@pytest.mark.parametrize("evento", [_fala_do_modelo(), _audio_do_modelo()])
async def test_para_quando_a_resposta_comeca(evento: Event) -> None:
    fonte = _Fonte(espera_s=0.001)
    frase = _frase(fonte)
    await frase.antes_da_ferramenta(BUSCA, {}, None)
    await asyncio.sleep(0.005)
    frase.observar(evento)
    await frase.fechar()
    assert fonte.limpezas == 1
    assert len(fonte.quadros) < 50


async def test_nao_toca_logo_depois_de_o_modelo_falar() -> None:
    fonte = _Fonte()
    frase = _frase(fonte)
    frase.observar(_fala_do_modelo())
    await frase.antes_da_ferramenta(BUSCA, {}, None)
    assert not frase.tocando


async def test_transcricao_atrasada_da_pergunta_nao_corta_a_frase() -> None:
    fonte = _Fonte(espera_s=0.001)
    frase = _frase(fonte)
    await frase.antes_da_ferramenta(BUSCA, {}, None)
    frase.observar(_fala_do_paciente("Quanto custa"))
    frase.observar(_fala_do_paciente(" a limpeza?", finished=True))
    assert frase.tocando
    assert fonte.limpezas == 0
    await frase.fechar()


async def test_para_quando_o_paciente_volta_a_falar() -> None:
    fonte = _Fonte(espera_s=0.001)
    frase = _frase(fonte)
    await frase.antes_da_ferramenta(BUSCA, {}, None)
    frase.observar(_fala_do_paciente("Quanto custa?", finished=True))
    frase.observar(_fala_do_paciente("Espera"))
    await frase.fechar()
    assert fonte.limpezas == 1


async def test_falha_ao_tocar_nao_chega_a_ferramenta() -> None:
    frase = _frase(_Fonte(erro=RuntimeError("sem faixa")))
    await frase.antes_da_ferramenta(BUSCA, {}, None)
    await _ate_o_fim(frase)
    await frase.fechar()


def _gravar(caminho: Path, canais: int = 1, largura: int = 2) -> None:
    with wave.open(str(caminho), "wb") as arquivo:
        arquivo.setnchannels(canais)
        arquivo.setsampwidth(largura)
        arquivo.setframerate(TAXA)
        arquivo.writeframes(b"\x00" * canais * largura * 80)


def test_carrega_wav_mono_de_16_bits(tmp_path: Path) -> None:
    caminho = tmp_path / "frase.wav"
    _gravar(caminho)
    audio = carregar_audio(caminho)
    assert audio is not None
    assert (audio.taxa, len(audio.amostras)) == (TAXA, 160)


def test_sem_arquivo_atende_sem_frase(tmp_path: Path) -> None:
    assert carregar_audio(tmp_path / "nao-existe.wav") is None


@pytest.mark.parametrize("opcoes", [{"canais": 2}, {"largura": 1}])
def test_formato_errado_atende_sem_frase(tmp_path: Path, opcoes: dict[str, int]) -> None:
    caminho = tmp_path / "frase.wav"
    _gravar(caminho, **opcoes)
    assert carregar_audio(caminho) is None


def test_arquivo_invalido_atende_sem_frase(tmp_path: Path) -> None:
    caminho = tmp_path / "frase.wav"
    caminho.write_bytes(b"isto nao e um wav")
    assert carregar_audio(caminho) is None


def _callbacks(agente: Any) -> list[Any]:
    return list(agente.before_tool_callback)


def test_agente_sem_frase_so_tem_a_trava() -> None:

    trava = TravaDeConfirmacao()
    chamada = ChamadaAberta("c", uuid4(), "Clínica")
    assert _callbacks(criar_agente(ConfigAgente(), chamada, trava)) == [trava.antes_da_ferramenta]


def test_frase_vem_antes_da_trava() -> None:

    trava = TravaDeConfirmacao()
    frase = _frase(_Fonte())
    agente = criar_agente(ConfigAgente(), ChamadaAberta("c", uuid4(), "C"), trava, frase)
    assert _callbacks(agente) == [frase.antes_da_ferramenta, trava.antes_da_ferramenta]
