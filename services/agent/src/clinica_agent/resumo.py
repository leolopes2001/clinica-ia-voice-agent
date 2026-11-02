"""Desfecho e transcrição de uma ligação, tirados dos eventos da sessão do ADK."""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from google.adk.events import Event

_DESFECHO_POR_FERRAMENTA = {
    "confirmar_agendamento": "agendou",
    "executar_remarcacao": "remarcou",
    "executar_cancelamento": "cancelou",
    "transfer_call": "transferiu",
}


@dataclass(frozen=True)
class ResumoDaLigacao:
    desfecho: str
    transcricao: str | None


def resultado_da_ferramenta(resposta: dict[str, Any] | None) -> dict[str, Any]:
    """O ADK entrega o resultado do MCP como `content`, com o JSON no primeiro texto."""
    if not resposta:
        return {}
    if isinstance(resposta.get("structuredContent"), dict):
        estruturado: dict[str, Any] = resposta["structuredContent"]
        return estruturado
    for parte in resposta.get("content") or []:
        try:
            lido = json.loads(parte.get("text", ""))
        except (TypeError, ValueError):
            continue
        if isinstance(lido, dict):
            return lido
    # Sem o envelope do MCP: é a recusa da trava de confirmação, que nem chega à ferramenta.
    return resposta


def resumir(eventos: Iterable[Event]) -> ResumoDaLigacao:
    linhas: list[str] = []
    desfecho: str | None = None
    consentiu = False
    paciente_falou = False
    for evento in eventos:
        if evento.input_transcription and (
            texto := (evento.input_transcription.text or "").strip()
        ):
            paciente_falou = True
            linhas.append(f"Paciente: {texto}")
        if evento.output_transcription and (
            texto := (evento.output_transcription.text or "").strip()
        ):
            linhas.append(f"Recepcionista: {texto}")
        for resposta in evento.get_function_responses():
            resultado = resultado_da_ferramenta(resposta.response)
            if resposta.name == "registrar_consentimento":
                consentiu = resultado.get("consentimento") is True
            elif resposta.name in _DESFECHO_POR_FERRAMENTA and resultado.get("ok", True):
                desfecho = _DESFECHO_POR_FERRAMENTA[resposta.name]
    return ResumoDaLigacao(
        desfecho=desfecho or ("duvida" if paciente_falou else "abandonou"),
        # A recusa ou a falta de resposta nem saem do agente. O worker confere de novo no banco.
        transcricao="\n".join(linhas) if consentiu and linhas else None,
    )
