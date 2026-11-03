from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

from clinica_api.esquemas import _Entrada

Perfil = Literal["admin", "recepcao"]
_Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, to_lower=True, pattern=r"^[^@\s]+@[^@\s]+$", max_length=254
    ),
]
_Senha = Annotated[str, StringConstraints(min_length=10, max_length=200)]


class LoginEntrada(_Entrada):
    email: _Email
    senha: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class Sessao(BaseModel):
    usuario_id: UUID
    clinica_id: UUID
    email: str
    perfil: str
    clinica: str
    fuso: str


class UsuarioCriar(_Entrada):
    email: _Email
    senha: _Senha
    perfil: Perfil


class UsuarioAtualizar(_Entrada):
    email: _Email
    perfil: Perfil
    ativo: bool
    # Vazia: mantém a senha.
    senha: _Senha | None = None


class UsuarioResumo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    perfil: str
    ativo: bool


class TrocarSenhaEntrada(_Entrada):
    atual: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    nova: _Senha


class RegistroDeAuditoria(BaseModel):
    criado_em: datetime
    usuario: str | None
    acao: str
    entidade: str
    antes: dict[str, Any] | None
    depois: dict[str, Any] | None


class ConsultaDaAgenda(BaseModel):
    id: UUID
    dentista_id: UUID
    dentista: str
    consultorio: str
    procedimento_id: UUID
    procedimento: str
    paciente_id: UUID | None
    paciente: str | None
    telefone: str | None
    inicio: datetime
    fim_periodo: datetime
    status: str
    origem: str
    motivo_cancelamento: str | None


class MudarStatusEntrada(_Entrada):
    status: Literal["faltou", "concluido"]


class PacienteEntrada(_Entrada):
    nome: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    telefone_e164: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)]
    data_nascimento: date


class PacienteResumo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    nome: str
    telefone_e164: str
    data_nascimento: date


class DentistaResumo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    nome: str
    especialidades: list[str]
    ativo: bool


class ProcedimentoCompleto(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    nome: str
    sinonimos: list[str]
    duracao_min: int
    intervalo_limpeza_min: int
    dentistas_habilitados: list[UUID]
