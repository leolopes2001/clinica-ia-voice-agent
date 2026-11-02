"""O que a ligação entrega ao agente antes de o modelo entrar: nada aqui passa pelo LLM."""

import json
from dataclasses import dataclass
from uuid import UUID

import httpx
from pydantic import BaseModel

CHAVE_CHAMADA = "chamada_id"
ATRIBUTO_TELEFONE_SIP = "sip.phoneNumber"


class SemTelefone(Exception):
    pass


class ClinicaNaoDefinida(Exception):
    pass


@dataclass(frozen=True)
class ChamadaAberta:
    chamada_id: str
    clinica_id: UUID
    clinica_nome: str


class _Clinica(BaseModel):
    clinica_id: UUID
    nome: str


def telefone_do_chamador(
    atributos: dict[str, str], eh_sip: bool, metadados_do_job: str | None, permitir_teste: bool
) -> str:
    """O SIP sempre vence. Só quem tem a chave do LiveKit consegue escrever os metadados."""
    if eh_sip:
        telefone = atributos.get(ATRIBUTO_TELEFONE_SIP)
    elif permitir_teste:
        telefone = json.loads(metadados_do_job or "{}").get("telefone_de_teste")
    else:
        telefone = None
    if not telefone:
        raise SemTelefone
    return str(telefone)


async def abrir_chamada(
    api: httpx.AsyncClient, clinica_id: UUID | None, telefone: str, sala: str
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
        json={"telefone": telefone, "sala": sala},
    )
    resposta.raise_for_status()
    return ChamadaAberta(resposta.json()["chamada_id"], clinica.clinica_id, clinica.nome)
