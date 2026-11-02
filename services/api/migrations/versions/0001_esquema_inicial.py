"""esquema inicial

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    # unaccent() é STABLE; com o dicionário explícito, o resultado é fixo e pode gerar coluna.
    op.execute(
        r"""
        CREATE FUNCTION normalizar_nome(nome text) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
        RETURN lower(
            trim(regexp_replace(
                public.unaccent('public.unaccent'::regdictionary, nome), '\s+', ' ', 'g'
            ))
        )
        """
    )
    op.create_table(
        "clinicas",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("nome", sa.Text(), nullable=False),
        sa.Column("telefone", sa.Text(), nullable=False),
        sa.Column("endereco", sa.Text(), nullable=False),
        sa.Column("fuso", sa.Text(), server_default="America/Sao_Paulo", nullable=False),
        sa.Column("politica_cancelamento_horas", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clinicas")),
    )
    op.create_table(
        "idempotencia",
        sa.Column("chave", sa.Text(), nullable=False),
        sa.Column("resposta_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("chave", name=op.f("pk_idempotencia")),
    )
    op.create_index("ix_idempotencia_criado_em", "idempotencia", ["criado_em"], unique=False)
    op.create_table(
        "conhecimento_documentos",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("clinica_id", sa.Uuid(), nullable=False),
        sa.Column("titulo", sa.Text(), nullable=False),
        sa.Column("arquivo", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="processando", nullable=False),
        sa.Column("ativo", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint(
            "status IN ('processando', 'pronto', 'erro')",
            name=op.f("ck_conhecimento_documentos_status_valido"),
        ),
        sa.ForeignKeyConstraint(
            ["clinica_id"],
            ["clinicas.id"],
            name=op.f("fk_conhecimento_documentos_clinica_id_clinicas"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conhecimento_documentos")),
    )
    op.create_table(
        "consultorios",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("clinica_id", sa.Uuid(), nullable=False),
        sa.Column("nome", sa.Text(), nullable=False),
        sa.Column("ativo", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(
            ["clinica_id"], ["clinicas.id"], name=op.f("fk_consultorios_clinica_id_clinicas")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consultorios")),
    )
    op.create_table(
        "dentistas",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("clinica_id", sa.Uuid(), nullable=False),
        sa.Column("nome", sa.Text(), nullable=False),
        sa.Column(
            "especialidades", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("ativo", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(
            ["clinica_id"], ["clinicas.id"], name=op.f("fk_dentistas_clinica_id_clinicas")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dentistas")),
    )
    op.create_table(
        "pacientes",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("clinica_id", sa.Uuid(), nullable=False),
        sa.Column("nome", sa.Text(), nullable=False),
        sa.Column(
            "nome_normalizado",
            sa.Text(),
            sa.Computed("normalizar_nome(nome)", persisted=True),
            nullable=False,
        ),
        sa.Column("telefone_e164", sa.Text(), nullable=False),
        sa.Column("data_nascimento", sa.Date(), nullable=False),
        sa.Column("consentimento_em", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["clinica_id"], ["clinicas.id"], name=op.f("fk_pacientes_clinica_id_clinicas")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pacientes")),
        sa.UniqueConstraint(
            "clinica_id",
            "telefone_e164",
            "nome_normalizado",
            "data_nascimento",
            name="uq_pacientes_mesma_pessoa",
        ),
    )
    op.create_index(
        "ix_pacientes_clinica_telefone", "pacientes", ["clinica_id", "telefone_e164"], unique=False
    )
    op.create_table(
        "procedimentos",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("clinica_id", sa.Uuid(), nullable=False),
        sa.Column("nome", sa.Text(), nullable=False),
        sa.Column("sinonimos", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("duracao_min", sa.Integer(), nullable=False),
        sa.Column(
            "intervalo_limpeza_min", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "dentistas_habilitados",
            postgresql.ARRAY(sa.Uuid()),
            server_default="{}",
            nullable=False,
        ),
        sa.CheckConstraint("duracao_min > 0", name=op.f("ck_procedimentos_duracao_positiva")),
        sa.CheckConstraint(
            "intervalo_limpeza_min >= 0", name=op.f("ck_procedimentos_intervalo_nao_negativo")
        ),
        sa.ForeignKeyConstraint(
            ["clinica_id"], ["clinicas.id"], name=op.f("fk_procedimentos_clinica_id_clinicas")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_procedimentos")),
    )
    op.create_table(
        "usuarios",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("clinica_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("senha_hash", sa.Text(), nullable=False),
        sa.Column("perfil", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "perfil IN ('admin', 'recepcao')", name=op.f("ck_usuarios_perfil_valido")
        ),
        sa.ForeignKeyConstraint(
            ["clinica_id"], ["clinicas.id"], name=op.f("fk_usuarios_clinica_id_clinicas")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usuarios")),
        sa.UniqueConstraint("email", name="uq_usuarios_email"),
    )
    op.create_table(
        "auditoria",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("clinica_id", sa.Uuid(), nullable=False),
        sa.Column("usuario_id", sa.Uuid(), nullable=True),
        sa.Column("acao", sa.Text(), nullable=False),
        sa.Column("entidade", sa.Text(), nullable=False),
        sa.Column("antes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("depois", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["clinica_id"], ["clinicas.id"], name=op.f("fk_auditoria_clinica_id_clinicas")
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"], ["usuarios.id"], name=op.f("fk_auditoria_usuario_id_usuarios")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auditoria")),
    )
    op.create_table(
        "bloqueios",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("clinica_id", sa.Uuid(), nullable=False),
        sa.Column("dentista_id", sa.Uuid(), nullable=True),
        sa.Column("periodo", postgresql.TSTZRANGE(), nullable=False),
        sa.Column("motivo", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["clinica_id"], ["clinicas.id"], name=op.f("fk_bloqueios_clinica_id_clinicas")
        ),
        sa.ForeignKeyConstraint(
            ["dentista_id"], ["dentistas.id"], name=op.f("fk_bloqueios_dentista_id_dentistas")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bloqueios")),
    )
    op.create_table(
        "chamadas",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("clinica_id", sa.Uuid(), nullable=False),
        sa.Column("telefone", sa.Text(), nullable=False),
        sa.Column("sala", sa.Text(), nullable=False),
        sa.Column(
            "inicio", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("fim", sa.DateTime(timezone=True), nullable=True),
        sa.Column("desfecho", sa.Text(), nullable=True),
        sa.Column("transcricao", sa.Text(), nullable=True),
        sa.Column("apagar_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paciente_id", sa.Uuid(), nullable=True),
        sa.Column("identidade_verificada_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "tentativas_identidade", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.CheckConstraint(
            "desfecho IN ('agendou', 'remarcou', 'cancelou', 'duvida', 'transferiu', 'abandonou')",
            name=op.f("ck_chamadas_desfecho_valido"),
        ),
        sa.CheckConstraint(
            "tentativas_identidade >= 0", name=op.f("ck_chamadas_tentativas_nao_negativas")
        ),
        sa.ForeignKeyConstraint(
            ["clinica_id"], ["clinicas.id"], name=op.f("fk_chamadas_clinica_id_clinicas")
        ),
        sa.ForeignKeyConstraint(
            ["paciente_id"], ["pacientes.id"], name=op.f("fk_chamadas_paciente_id_pacientes")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chamadas")),
    )
    op.create_table(
        "conhecimento_trechos",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("clinica_id", sa.Uuid(), nullable=False),
        sa.Column("documento_id", sa.Uuid(), nullable=False),
        sa.Column("ordem", sa.Integer(), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(768), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('portuguese', texto)", persisted=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["clinica_id"],
            ["clinicas.id"],
            name=op.f("fk_conhecimento_trechos_clinica_id_clinicas"),
        ),
        sa.ForeignKeyConstraint(
            ["documento_id"],
            ["conhecimento_documentos.id"],
            name=op.f("fk_conhecimento_trechos_documento_id_conhecimento_documentos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conhecimento_trechos")),
        sa.UniqueConstraint(
            "documento_id", "ordem", name="uq_conhecimento_trechos_documento_ordem"
        ),
    )
    op.create_index(
        "ix_conhecimento_trechos_embedding",
        "conhecimento_trechos",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_conhecimento_trechos_tsv",
        "conhecimento_trechos",
        ["tsv"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_table(
        "grade_trabalho",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("clinica_id", sa.Uuid(), nullable=False),
        sa.Column("dentista_id", sa.Uuid(), nullable=False),
        sa.Column("dia_semana", sa.Integer(), nullable=False),
        sa.Column("inicio", sa.Time(), nullable=False),
        sa.Column("fim", sa.Time(), nullable=False),
        sa.Column("consultorio_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "dia_semana BETWEEN 0 AND 6", name=op.f("ck_grade_trabalho_dia_semana_valido")
        ),
        sa.CheckConstraint("inicio < fim", name=op.f("ck_grade_trabalho_inicio_antes_do_fim")),
        sa.ForeignKeyConstraint(
            ["clinica_id"], ["clinicas.id"], name=op.f("fk_grade_trabalho_clinica_id_clinicas")
        ),
        sa.ForeignKeyConstraint(
            ["consultorio_id"],
            ["consultorios.id"],
            name=op.f("fk_grade_trabalho_consultorio_id_consultorios"),
        ),
        sa.ForeignKeyConstraint(
            ["dentista_id"], ["dentistas.id"], name=op.f("fk_grade_trabalho_dentista_id_dentistas")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_grade_trabalho")),
    )
    op.create_table(
        "agendamentos",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("clinica_id", sa.Uuid(), nullable=False),
        sa.Column("dentista_id", sa.Uuid(), nullable=False),
        sa.Column("consultorio_id", sa.Uuid(), nullable=False),
        sa.Column("paciente_id", sa.Uuid(), nullable=True),
        sa.Column("procedimento_id", sa.Uuid(), nullable=False),
        sa.Column("periodo", postgresql.TSTZRANGE(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expira_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("chamada_id", sa.Uuid(), nullable=True),
        sa.Column("origem", sa.Text(), nullable=False),
        sa.Column("motivo_cancelamento", sa.Text(), nullable=True),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        postgresql.ExcludeConstraint(
            (sa.column("consultorio_id"), "="),
            (sa.column("periodo"), "&&"),
            where=sa.text("status IN ('segurado', 'confirmado')"),
            using="gist",
            name="sem_conflito_consultorio",
        ),
        postgresql.ExcludeConstraint(
            (sa.column("dentista_id"), "="),
            (sa.column("periodo"), "&&"),
            where=sa.text("status IN ('segurado', 'confirmado')"),
            using="gist",
            name="sem_conflito_dentista",
        ),
        sa.CheckConstraint(
            "origem IN ('voz', 'painel')", name=op.f("ck_agendamentos_origem_valida")
        ),
        sa.CheckConstraint(
            "status <> 'confirmado' OR paciente_id IS NOT NULL",
            name=op.f("ck_agendamentos_confirmado_exige_paciente"),
        ),
        sa.CheckConstraint(
            "status <> 'segurado' OR expira_em IS NOT NULL",
            name=op.f("ck_agendamentos_segurado_exige_expiracao"),
        ),
        sa.CheckConstraint(
            "status IN ('segurado', 'confirmado', 'cancelado', 'expirado', 'faltou', 'concluido')",
            name=op.f("ck_agendamentos_status_valido"),
        ),
        sa.ForeignKeyConstraint(
            ["chamada_id"], ["chamadas.id"], name=op.f("fk_agendamentos_chamada_id_chamadas")
        ),
        sa.ForeignKeyConstraint(
            ["clinica_id"], ["clinicas.id"], name=op.f("fk_agendamentos_clinica_id_clinicas")
        ),
        sa.ForeignKeyConstraint(
            ["consultorio_id"],
            ["consultorios.id"],
            name=op.f("fk_agendamentos_consultorio_id_consultorios"),
        ),
        sa.ForeignKeyConstraint(
            ["dentista_id"], ["dentistas.id"], name=op.f("fk_agendamentos_dentista_id_dentistas")
        ),
        sa.ForeignKeyConstraint(
            ["paciente_id"], ["pacientes.id"], name=op.f("fk_agendamentos_paciente_id_pacientes")
        ),
        sa.ForeignKeyConstraint(
            ["procedimento_id"],
            ["procedimentos.id"],
            name=op.f("fk_agendamentos_procedimento_id_procedimentos"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agendamentos")),
    )
    op.create_table(
        "pedidos_retorno",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("clinica_id", sa.Uuid(), nullable=False),
        sa.Column("protocolo", sa.Text(), nullable=False),
        sa.Column("chamada_id", sa.Uuid(), nullable=True),
        sa.Column("telefone", sa.Text(), nullable=False),
        sa.Column("motivo", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pendente", nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pendente', 'concluido')", name=op.f("ck_pedidos_retorno_status_valido")
        ),
        sa.ForeignKeyConstraint(
            ["chamada_id"], ["chamadas.id"], name=op.f("fk_pedidos_retorno_chamada_id_chamadas")
        ),
        sa.ForeignKeyConstraint(
            ["clinica_id"], ["clinicas.id"], name=op.f("fk_pedidos_retorno_clinica_id_clinicas")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pedidos_retorno")),
        sa.UniqueConstraint("protocolo", name="uq_pedidos_retorno_protocolo"),
    )


def downgrade() -> None:
    op.drop_table("pedidos_retorno")
    op.drop_table("agendamentos")
    op.drop_table("grade_trabalho")
    op.drop_index(
        "ix_conhecimento_trechos_tsv", table_name="conhecimento_trechos", postgresql_using="gin"
    )
    op.drop_index(
        "ix_conhecimento_trechos_embedding",
        table_name="conhecimento_trechos",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.drop_table("conhecimento_trechos")
    op.drop_table("chamadas")
    op.drop_table("bloqueios")
    op.drop_table("auditoria")
    op.drop_table("usuarios")
    op.drop_table("procedimentos")
    op.drop_index("ix_pacientes_clinica_telefone", table_name="pacientes")
    op.drop_table("pacientes")
    op.drop_table("dentistas")
    op.drop_table("consultorios")
    op.drop_table("conhecimento_documentos")
    op.drop_index("ix_idempotencia_criado_em", table_name="idempotencia")
    op.drop_table("idempotencia")
    op.drop_table("clinicas")
    op.execute("DROP FUNCTION normalizar_nome(text)")
    op.execute("DROP EXTENSION unaccent")
    op.execute("DROP EXTENSION vector")
    op.execute("DROP EXTENSION btree_gist")
