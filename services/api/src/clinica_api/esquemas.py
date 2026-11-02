from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict


class _Entrada(BaseModel):
    # Campos a mais são recusados: telefone e clínica nunca vêm no corpo.
    model_config = ConfigDict(extra="forbid")


class SegurarEntrada(_Entrada):
    procedimento_id: UUID
    dentista_id: UUID
    inicio: AwareDatetime


class ConfirmarEntrada(_Entrada):
    # Só no painel. Na ligação, o paciente vem da chamada.
    paciente_id: UUID | None = None


class RemarcarEntrada(_Entrada):
    hold_id: UUID


class CancelarEntrada(_Entrada):
    motivo: str | None = None


class Horario(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    dentista_id: UUID
    consultorio_id: UUID
    inicio: datetime
    fim: datetime
    fim_periodo: datetime


class HorariosLivres(BaseModel):
    horarios: list[Horario]


class Falha(BaseModel):
    ok: Literal[False] = False
    motivo: str
    alternativas: list[Horario] = []


class HoldCriado(BaseModel):
    ok: Literal[True] = True
    hold_id: UUID
    expira_em: datetime
    dentista_id: UUID
    consultorio_id: UUID
    procedimento_id: UUID
    inicio: datetime
    fim_periodo: datetime


class AgendamentoConfirmado(BaseModel):
    ok: Literal[True] = True
    agendamento_id: UUID
    paciente_id: UUID
    dentista_id: UUID
    consultorio_id: UUID
    procedimento_id: UUID
    inicio: datetime
    fim_periodo: datetime


class AgendamentoCancelado(BaseModel):
    ok: Literal[True] = True
    agendamento_id: UUID
    dentro_do_prazo: bool
    politica_cancelamento_horas: int
