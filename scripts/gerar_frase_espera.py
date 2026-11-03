"""Gera o áudio da frase de espera (T3.7) em infra/audio/frase_espera.wav.

`--motor say` usa a voz do macOS, sem custo, só para testar o caminho do áudio.
`--motor gemini` usa a voz do atendimento e gasta crédito: é o áudio para valer.
"""

import argparse
import base64
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any

FRASE = "Só um instante, vou verificar."
RAIZ = Path(__file__).resolve().parent.parent
DESTINO = RAIZ / "infra" / "audio" / "frase_espera.wav"
TAXA = 24000


def _gerar_com_say(destino: Path) -> None:
    with tempfile.TemporaryDirectory() as pasta:
        bruto = Path(pasta) / "frase.aiff"
        subprocess.run(["say", "-v", "Luciana", "-o", str(bruto), FRASE], check=True)
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", f"LEI16@{TAXA}", "-c", "1", str(bruto), str(destino)],
            check=True,
        )


def _gerar_com_gemini(destino: Path, modelo: str, voz: str) -> None:
    from google import genai

    resposta: Any = genai.Client().interactions.create(
        model=modelo,
        input=[{"type": "user_input", "content": [{"type": "text", "text": FRASE}]}],
        response_format={"type": "audio"},
        generation_config={"speech_config": [{"voice": voz}]},
    )
    destino.write_bytes(base64.b64decode(resposta.output_audio.data))


def _conferir(destino: Path) -> None:
    with wave.open(str(destino), "rb") as arquivo:
        if arquivo.getnchannels() != 1 or arquivo.getsampwidth() != 2:
            raise SystemExit("O áudio gerado precisa ser mono e de 16 bits")
        print(f"{destino}: {arquivo.getnframes() / arquivo.getframerate():.2f} s")


if __name__ == "__main__":
    argumentos = argparse.ArgumentParser(description=__doc__)
    argumentos.add_argument("--motor", choices=["say", "gemini"], required=True)
    argumentos.add_argument("--modelo", default="gemini-3.8-flash-tts")
    argumentos.add_argument("--voz", default="Kore", help="A mesma voz configurada no painel.")
    opcoes = argumentos.parse_args()
    if opcoes.motor == "say":
        _gerar_com_say(DESTINO)
    else:
        _gerar_com_gemini(DESTINO, opcoes.modelo, opcoes.voz)
    _conferir(DESTINO)
