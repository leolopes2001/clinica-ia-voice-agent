"""O que a clínica configura no painel. Sem linha ou com campo vazio, vale o padrão."""

from datetime import datetime, time
from typing import Annotated, Any, Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_api import auditoria
from clinica_api.agenda import ErroAgenda
from clinica_api.autenticacao import Admin, Sessao
from clinica_api.esquemas import _Entrada
from clinica_core.config import ConfigAgenda
from clinica_core.modelos import ConfiguracaoAgente
from clinica_core.telefone import normalizar_telefone

router = APIRouter(prefix="/painel")

# Vozes da API Live do Gemini. Testar de ouvido: a lista muda com o modelo.
VOZES = ("Aoede", "Charon", "Fenrir", "Kore", "Leda", "Orus", "Puck", "Zephyr")
Voz = Literal["Aoede", "Charon", "Fenrir", "Kore", "Leda", "Orus", "Puck", "Zephyr"]
_DIAS = {str(dia) for dia in range(7)}


class FaixaDoDia(BaseModel):
    inicio: time
    fim: time

    @model_validator(mode="after")
    def _inicio_antes_do_fim(self) -> Self:
        if self.inicio >= self.fim:
            raise ValueError("inicio precisa vir antes de fim")
        return self


class ConfiguracaoEntrada(_Entrada):
    saudacao: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    voz: Voz | None = None
    # A chave é o dia da semana, com 0 na segunda. Dia fora daqui: fechado.
    horario_comercial: dict[str, FaixaDoDia] | None = None
    numero_transferencia: Annotated[str, StringConstraints(strip_whitespace=True)] | None = None
    antecedencia_minima_min: int | None = Field(default=None, ge=0, le=43200)

    @field_validator("horario_comercial")
    @classmethod
    def _dias_validos(cls, valor: dict[str, FaixaDoDia] | None) -> dict[str, FaixaDoDia] | None:
        if valor is not None and not set(valor) <= _DIAS:
            raise ValueError("dia da semana inválido")
        return valor


class ConfiguracaoResumo(BaseModel):
    saudacao: str | None
    voz: str | None
    horario_comercial: dict[str, FaixaDoDia] | None
    numero_transferencia: str | None
    antecedencia_minima_min: int | None
    antecedencia_padrao_min: int
    vozes: list[str]


class ConfiguracaoDaLigacao(BaseModel):
    """O que o agente precisa ao atender. O número de transferência não vai ao modelo."""

    saudacao: str | None
    voz: str | None
    dentro_do_horario: bool
    numero_transferencia: str | None
    retencao_dias: int


def destino_de_transferencia(bruto: str) -> str:
    """E.164, ou uma URI SIP para um ramal: nunca um texto livre que vá parar no REFER."""
    if bruto.lower().startswith("sip:"):
        return bruto
    telefone = normalizar_telefone(bruto)
    if telefone is None:
        raise ErroAgenda("numero_transferencia_invalido")
    return telefone


def dentro_do_horario(horario: dict[str, Any] | None, agora: datetime, fuso: ZoneInfo) -> bool:
    # Sem horário cadastrado, nada é "fora do horário": o agente não inventa que a clínica fechou.
    if not horario:
        return True
    local = agora.astimezone(fuso)
    faixa = horario.get(str(local.weekday()))
    if faixa is None:
        return False
    return time.fromisoformat(faixa["inicio"]) <= local.time() < time.fromisoformat(faixa["fim"])


async def configuracao_da_agenda(
    sessao: AsyncSession, clinica_id: UUID, padrao: ConfigAgenda
) -> ConfigAgenda:
    antecedencia = await sessao.scalar(
        select(ConfiguracaoAgente.antecedencia_minima_min).where(
            ConfiguracaoAgente.clinica_id == clinica_id
        )
    )
    if antecedencia is None:
        return padrao
    return padrao.model_copy(update={"antecedencia_minima_min": antecedencia})


def _resumo(linha: ConfiguracaoAgente | None, padrao: ConfigAgenda) -> ConfiguracaoResumo:
    return ConfiguracaoResumo(
        saudacao=linha.saudacao if linha else None,
        voz=linha.voz if linha else None,
        horario_comercial=linha.horario_comercial if linha else None,
        numero_transferencia=linha.numero_transferencia if linha else None,
        antecedencia_minima_min=linha.antecedencia_minima_min if linha else None,
        antecedencia_padrao_min=padrao.antecedencia_minima_min,
        vozes=list(VOZES),
    )


@router.get("/configuracoes")
async def ver_configuracoes(request: Request, sessao: Sessao, atual: Admin) -> ConfiguracaoResumo:
    linha = await sessao.get(ConfiguracaoAgente, atual.clinica_id)
    return _resumo(linha, request.app.state.config_agenda)


@router.put("/configuracoes")
async def salvar_configuracoes(
    entrada: ConfiguracaoEntrada, request: Request, sessao: Sessao, atual: Admin
) -> ConfiguracaoResumo:
    padrao: ConfigAgenda = request.app.state.config_agenda
    linha = await sessao.get(ConfiguracaoAgente, atual.clinica_id, with_for_update=True)
    antes = _resumo(linha, padrao).model_dump(mode="json", exclude={"vozes"})
    if linha is None:
        linha = ConfiguracaoAgente(clinica_id=atual.clinica_id)
        sessao.add(linha)
    numero = entrada.numero_transferencia
    linha.saudacao = entrada.saudacao or None
    linha.voz = entrada.voz
    linha.horario_comercial = (
        {dia: faixa.model_dump(mode="json") for dia, faixa in entrada.horario_comercial.items()}
        if entrada.horario_comercial
        else None
    )
    linha.numero_transferencia = destino_de_transferencia(numero) if numero else None
    linha.antecedencia_minima_min = entrada.antecedencia_minima_min
    await sessao.flush()
    resposta = _resumo(linha, padrao)
    auditoria.registrar(
        sessao,
        atual.clinica_id,
        atual.id,
        "atualizar",
        "configuracao_agente",
        antes=antes,
        depois=resposta.model_dump(mode="json", exclude={"vozes"}),
    )
    await sessao.commit()
    return resposta
