from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import (
    ARRAY,
    JSONB,
    TSTZRANGE,
    TSVECTOR,
    ExcludeConstraint,
    Range,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DIMENSAO_EMBEDDING = 768
_STATUS_ATIVOS = text("status IN ('segurado', 'confirmado')")


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    type_annotation_map = {str: Text, datetime: DateTime(timezone=True)}


def _id() -> Mapped[UUID]:
    return mapped_column(primary_key=True, server_default=func.gen_random_uuid())


def _clinica_id() -> Mapped[UUID]:
    return mapped_column(ForeignKey("clinicas.id"))


def _criado_em() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


class Clinica(Base):
    __tablename__ = "clinicas"

    id: Mapped[UUID] = _id()
    nome: Mapped[str]
    telefone: Mapped[str]
    endereco: Mapped[str]
    fuso: Mapped[str] = mapped_column(server_default="America/Sao_Paulo")
    politica_cancelamento_horas: Mapped[int]


class Dentista(Base):
    __tablename__ = "dentistas"

    id: Mapped[UUID] = _id()
    clinica_id: Mapped[UUID] = _clinica_id()
    nome: Mapped[str]
    especialidades: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    ativo: Mapped[bool] = mapped_column(server_default=text("true"))


class Consultorio(Base):
    __tablename__ = "consultorios"

    id: Mapped[UUID] = _id()
    clinica_id: Mapped[UUID] = _clinica_id()
    nome: Mapped[str]
    ativo: Mapped[bool] = mapped_column(server_default=text("true"))


class Procedimento(Base):
    __tablename__ = "procedimentos"
    __table_args__ = (
        CheckConstraint("duracao_min > 0", name="duracao_positiva"),
        CheckConstraint("intervalo_limpeza_min >= 0", name="intervalo_nao_negativo"),
    )

    id: Mapped[UUID] = _id()
    clinica_id: Mapped[UUID] = _clinica_id()
    nome: Mapped[str]
    sinonimos: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    duracao_min: Mapped[int]
    intervalo_limpeza_min: Mapped[int] = mapped_column(server_default=text("0"))
    dentistas_habilitados: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid), server_default="{}")


class GradeTrabalho(Base):
    __tablename__ = "grade_trabalho"
    __table_args__ = (
        # 0 = segunda-feira, como em date.weekday().
        CheckConstraint("dia_semana BETWEEN 0 AND 6", name="dia_semana_valido"),
        CheckConstraint("inicio < fim", name="inicio_antes_do_fim"),
    )

    id: Mapped[UUID] = _id()
    clinica_id: Mapped[UUID] = _clinica_id()
    dentista_id: Mapped[UUID] = mapped_column(ForeignKey("dentistas.id"))
    dia_semana: Mapped[int]
    inicio: Mapped[time]
    fim: Mapped[time]
    consultorio_id: Mapped[UUID] = mapped_column(ForeignKey("consultorios.id"))


class Bloqueio(Base):
    __tablename__ = "bloqueios"

    id: Mapped[UUID] = _id()
    clinica_id: Mapped[UUID] = _clinica_id()
    # Nulo = clínica toda.
    dentista_id: Mapped[UUID | None] = mapped_column(ForeignKey("dentistas.id"))
    periodo: Mapped[Range[datetime]] = mapped_column(TSTZRANGE)
    motivo: Mapped[str]


class Paciente(Base):
    __tablename__ = "pacientes"
    __table_args__ = (
        Index("ix_pacientes_clinica_telefone", "clinica_id", "telefone_e164"),
        UniqueConstraint(
            "clinica_id",
            "telefone_e164",
            "nome_normalizado",
            "data_nascimento",
            name="uq_pacientes_mesma_pessoa",
        ),
    )

    id: Mapped[UUID] = _id()
    clinica_id: Mapped[UUID] = _clinica_id()
    nome: Mapped[str]
    # normalizar_nome é criada na migration.
    nome_normalizado: Mapped[str] = mapped_column(Computed("normalizar_nome(nome)", persisted=True))
    telefone_e164: Mapped[str]
    data_nascimento: Mapped[date]
    consentimento_em: Mapped[datetime | None]


class Chamada(Base):
    __tablename__ = "chamadas"
    __table_args__ = (
        CheckConstraint(
            "desfecho IN ('agendou', 'remarcou', 'cancelou', 'duvida', 'transferiu', 'abandonou')",
            name="desfecho_valido",
        ),
        CheckConstraint("tentativas_identidade >= 0", name="tentativas_nao_negativas"),
    )

    id: Mapped[UUID] = _id()
    clinica_id: Mapped[UUID] = _clinica_id()
    telefone: Mapped[str]
    sala: Mapped[str]
    inicio: Mapped[datetime] = mapped_column(server_default=func.now())
    fim: Mapped[datetime | None]
    desfecho: Mapped[str | None]
    transcricao: Mapped[str | None]
    apagar_em: Mapped[datetime | None]
    paciente_id: Mapped[UUID | None] = mapped_column(ForeignKey("pacientes.id"))
    identidade_verificada_em: Mapped[datetime | None]
    tentativas_identidade: Mapped[int] = mapped_column(server_default=text("0"))
    consentimento_em: Mapped[datetime | None]
    consentimento_recusado_em: Mapped[datetime | None]


class Agendamento(Base):
    __tablename__ = "agendamentos"
    __table_args__ = (
        CheckConstraint(
            "status IN ('segurado', 'confirmado', 'cancelado', 'expirado', 'faltou', 'concluido')",
            name="status_valido",
        ),
        CheckConstraint("origem IN ('voz', 'painel')", name="origem_valida"),
        CheckConstraint(
            "status <> 'segurado' OR expira_em IS NOT NULL", name="segurado_exige_expiracao"
        ),
        CheckConstraint(
            "status <> 'confirmado' OR paciente_id IS NOT NULL", name="confirmado_exige_paciente"
        ),
        CheckConstraint("origem <> 'voz' OR chamada_id IS NOT NULL", name="voz_exige_chamada"),
        ExcludeConstraint(
            ("dentista_id", "="),
            ("periodo", "&&"),
            name="sem_conflito_dentista",
            using="gist",
            where=_STATUS_ATIVOS,
        ),
        ExcludeConstraint(
            ("consultorio_id", "="),
            ("periodo", "&&"),
            name="sem_conflito_consultorio",
            using="gist",
            where=_STATUS_ATIVOS,
        ),
    )

    id: Mapped[UUID] = _id()
    clinica_id: Mapped[UUID] = _clinica_id()
    dentista_id: Mapped[UUID] = mapped_column(ForeignKey("dentistas.id"))
    consultorio_id: Mapped[UUID] = mapped_column(ForeignKey("consultorios.id"))
    # Nulo enquanto segurado.
    paciente_id: Mapped[UUID | None] = mapped_column(ForeignKey("pacientes.id"))
    procedimento_id: Mapped[UUID] = mapped_column(ForeignKey("procedimentos.id"))
    # [inicio, fim + intervalo_limpeza)
    periodo: Mapped[Range[datetime]] = mapped_column(TSTZRANGE)
    status: Mapped[str]
    # Só para 'segurado'.
    expira_em: Mapped[datetime | None]
    # Quem segurou.
    chamada_id: Mapped[UUID | None] = mapped_column(ForeignKey("chamadas.id"))
    origem: Mapped[str]
    motivo_cancelamento: Mapped[str | None]
    criado_em: Mapped[datetime] = _criado_em()


class PedidoRetorno(Base):
    __tablename__ = "pedidos_retorno"
    __table_args__ = (
        UniqueConstraint("protocolo", name="uq_pedidos_retorno_protocolo"),
        CheckConstraint("status IN ('pendente', 'concluido')", name="status_valido"),
    )

    id: Mapped[UUID] = _id()
    clinica_id: Mapped[UUID] = _clinica_id()
    protocolo: Mapped[str]
    chamada_id: Mapped[UUID | None] = mapped_column(ForeignKey("chamadas.id"))
    telefone: Mapped[str]
    motivo: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="pendente")
    criado_em: Mapped[datetime] = _criado_em()


class ConhecimentoDocumento(Base):
    __tablename__ = "conhecimento_documentos"
    __table_args__ = (
        CheckConstraint("status IN ('processando', 'pronto', 'erro')", name="status_valido"),
    )

    id: Mapped[UUID] = _id()
    clinica_id: Mapped[UUID] = _clinica_id()
    titulo: Mapped[str]
    # Nulo quando o texto é editado direto no painel.
    arquivo: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default="processando")
    ativo: Mapped[bool] = mapped_column(server_default=text("true"))


class ConhecimentoTrecho(Base):
    __tablename__ = "conhecimento_trechos"
    __table_args__ = (
        UniqueConstraint("documento_id", "ordem", name="uq_conhecimento_trechos_documento_ordem"),
        Index(
            "ix_conhecimento_trechos_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_conhecimento_trechos_tsv", "tsv", postgresql_using="gin"),
    )

    id: Mapped[UUID] = _id()
    clinica_id: Mapped[UUID] = _clinica_id()
    documento_id: Mapped[UUID] = mapped_column(
        ForeignKey("conhecimento_documentos.id", ondelete="CASCADE")
    )
    ordem: Mapped[int]
    texto: Mapped[str]
    embedding: Mapped[list[float]] = mapped_column(Vector(DIMENSAO_EMBEDDING))
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('portuguese', texto)", persisted=True)
    )


class Idempotencia(Base):
    __tablename__ = "idempotencia"
    __table_args__ = (Index("ix_idempotencia_criado_em", "criado_em"),)

    chave: Mapped[str] = mapped_column(primary_key=True)
    resposta_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    criado_em: Mapped[datetime] = _criado_em()


class Usuario(Base):
    __tablename__ = "usuarios"
    __table_args__ = (
        UniqueConstraint("email", name="uq_usuarios_email"),
        CheckConstraint("perfil IN ('admin', 'recepcao')", name="perfil_valido"),
    )

    id: Mapped[UUID] = _id()
    clinica_id: Mapped[UUID] = _clinica_id()
    email: Mapped[str]
    senha_hash: Mapped[str]
    perfil: Mapped[str]
    ativo: Mapped[bool] = mapped_column(server_default=text("true"))
    falhas_de_login: Mapped[int] = mapped_column(server_default=text("0"))
    bloqueado_ate: Mapped[datetime | None]


class Auditoria(Base):
    __tablename__ = "auditoria"

    id: Mapped[UUID] = _id()
    clinica_id: Mapped[UUID] = _clinica_id()
    usuario_id: Mapped[UUID | None] = mapped_column(ForeignKey("usuarios.id"))
    acao: Mapped[str]
    entidade: Mapped[str]
    antes: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    depois: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    criado_em: Mapped[datetime] = _criado_em()
