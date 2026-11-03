"""O que a ligação entrega ao agente antes de o modelo entrar: nada aqui passa pelo LLM."""

import json
from dataclasses import dataclass, field
from uuid import UUID

import httpx
from pydantic import BaseModel

from clinica_core.telefone import normalizar_telefone

CHAVE_CHAMADA = "chamada_id"
ATRIBUTO_TELEFONE_SIP = "sip.phoneNumber"


class ClinicaNaoDefinida(Exception):
    pass


class ConfiguracaoDaLigacao(BaseModel):
    """Vem da API, ao atender. O número de transferência nunca vai ao modelo."""

    saudacao: str | None = None
    voz: str | None = None
    dentro_do_horario: bool = True
    numero_transferencia: str | None = None
    retencao_dias: int = 90
    telefone_oculto: bool = False


@dataclass(frozen=True)
class ChamadaAberta:
    chamada_id: str
    clinica_id: UUID
    clinica_nome: str
    configuracao: ConfiguracaoDaLigacao = field(default_factory=ConfiguracaoDaLigacao)


class _Clinica(BaseModel):
    clinica_id: UUID
    nome: str


def telefone_do_chamador(
    atributos: dict[str, str], eh_sip: bool, metadados_do_job: str | None, permitir_teste: bool
) -> str | None:
    """O SIP sempre vence. Sem um número confiável, a chamada segue como número oculto."""
    if eh_sip:
        bruto = atributos.get(ATRIBUTO_TELEFONE_SIP)
    elif permitir_teste:
        # Só quem tem a chave do LiveKit consegue escrever os metadados.
        bruto = json.loads(metadados_do_job or "{}").get("telefone_de_teste")
    else:
        bruto = None
    return normalizar_telefone(str(bruto)) if bruto else None


async def abrir_chamada(
    api: httpx.AsyncClient, clinica_id: UUID | None, telefone: str | None, sala: str
) -> ChamadaAberta:
    resposta = await api.get("/clinicas")
    resposta.raise_for_status()
    clinicas = [_Clinica.model_validate(c) for c in resposta.json()["clinicas"]]
    if clinica_id is not None:
        clinicas = [c for c in clinicas if c.clinica_id == clinica_id]
    if len(clinicas) != 1:
        raise ClinicaNaoDefinida(f"{len(clinicas)} clínicas encontradas: rode o seed ou configure")
    clinica = clinicas[0]
    resposta = await api.post(
        "/chamadas",
        # A sala como chave: um despacho repetido não duplica a chamada.
        headers={"X-Clinica-Id": str(clinica.clinica_id), "Idempotency-Key": sala},
        json={"sala": sala, **({"telefone": telefone} if telefone else {})},
    )
    resposta.raise_for_status()
    chamada_id = resposta.json()["chamada_id"]
    resposta = await api.get("/chamadas/configuracao", headers={"X-Chamada-Id": chamada_id})
    resposta.raise_for_status()
    return ChamadaAberta(
        chamada_id,
        clinica.clinica_id,
        clinica.nome,
        ConfiguracaoDaLigacao.model_validate(resposta.json()),
    )
