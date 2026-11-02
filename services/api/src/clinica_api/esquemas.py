from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints, computed_field

Turno = Literal["manha", "tarde", "noite"]
_Texto = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


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


class SegurarPorNomeEntrada(_Entrada):
    procedimento: _Texto
    dentista: _Texto
    inicio: AwareDatetime


class ClinicaResumo(BaseModel):
    clinica_id: UUID
    nome: str


class Clinicas(BaseModel):
    clinicas: list[ClinicaResumo]


class CriarChamadaEntrada(_Entrada):
    telefone: Annotated[str, StringConstraints(pattern=r"^\+[1-9]\d{7,14}$")]
    sala: _Texto


class ConsentimentoEntrada(_Entrada):
    aceitou: bool


class ConsentimentoRegistrado(BaseModel):
    consentimento: bool


class SelecionarPacienteEntrada(_Entrada):
    paciente_id: UUID


class CadastrarPacienteEntrada(_Entrada):
    nome: _Texto
    data_nascimento: date


class VerificarIdentidadeEntrada(_Entrada):
    data_nascimento: date


class PedidoRetornoEntrada(_Entrada):
    motivo: _Texto


class Horario(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    dentista_id: UUID
    consultorio_id: UUID
    inicio: datetime
    fim: datetime
    fim_periodo: datetime


class HorariosLivres(BaseModel):
    horarios: list[Horario]


class Opcao(BaseModel):
    dentista_id: UUID
    dentista: str
    inicio: datetime
    fala: str


class OpcoesHorario(BaseModel):
    opcoes: list[Opcao]


class Falha(BaseModel):
    ok: Literal[False] = False
    motivo: str
    alternativas: list[Horario] = []
    # Só na rota por nome: as até 3 alternativas que o agente fala ao paciente.
    opcoes: list[Opcao] = []


class HoldCriado(BaseModel):
    ok: Literal[True] = True
    hold_id: UUID
    expira_em: datetime
    dentista_id: UUID
    consultorio_id: UUID
    procedimento_id: UUID
    inicio: datetime
    fim_periodo: datetime


class HoldFalado(HoldCriado):
    dentista: str
    procedimento: str
    fala: str


class AgendamentoConfirmado(BaseModel):
    ok: Literal[True] = True
    agendamento_id: UUID
    paciente_id: UUID
    dentista_id: UUID
    consultorio_id: UUID
    procedimento_id: UUID
    inicio: datetime
    fim_periodo: datetime


class CancelamentoPreparado(BaseModel):
    ok: Literal[True] = True
    agendamento_id: UUID
    fala: str
    dentro_do_prazo: bool
    politica_cancelamento_horas: int


class RemarcacaoPreparada(BaseModel):
    ok: Literal[True] = True
    agendamento_id: UUID
    hold_id: UUID
    fala_atual: str
    fala_nova: str


class AgendamentoCancelado(BaseModel):
    ok: Literal[True] = True
    agendamento_id: UUID
    dentro_do_prazo: bool
    politica_cancelamento_horas: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def aviso(self) -> str:
        horas = self.politica_cancelamento_horas
        if self.dentro_do_prazo:
            return f"Cancelado dentro do prazo de {horas} horas da política da clínica."
        return f"Cancelado com menos de {horas} horas de antecedência, fora do prazo da política."


class ChamadaCriada(BaseModel):
    chamada_id: UUID


class PacienteDaChamada(BaseModel):
    # Só o primeiro nome: o agente não precisa de mais nada para cumprimentar (LGPD).
    paciente_id: UUID
    primeiro_nome: str


class PacientesDoTelefone(BaseModel):
    pacientes: list[PacienteDaChamada]


class ResultadoVerificacao(BaseModel):
    ok: bool
    tentativas_restantes: int
    motivo: str | None = None


class ProcedimentoResumo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    nome: str
    duracao_min: int
    sinonimos: list[str]


class Procedimentos(BaseModel):
    procedimentos: list[ProcedimentoResumo]


class ConsultaFutura(BaseModel):
    agendamento_id: UUID
    procedimento: str
    dentista: str
    inicio: datetime
    fala: str


class ConsultasFuturas(BaseModel):
    consultas: list[ConsultaFutura]


class PedidoRetornoCriado(BaseModel):
    ok: Literal[True] = True
    protocolo: str
