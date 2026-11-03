"""configurações do agente, editadas no painel

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "configuracoes_agente",
        sa.Column(
            "clinica_id",
            sa.Uuid(),
            sa.ForeignKey("clinicas.id", name="fk_configuracoes_agente_clinica_id_clinicas"),
            primary_key=True,
        ),
        sa.Column("saudacao", sa.Text()),
        sa.Column("voz", sa.Text()),
        sa.Column("horario_comercial", JSONB()),
        sa.Column("numero_transferencia", sa.Text()),
        sa.Column("antecedencia_minima_min", sa.Integer()),
        sa.CheckConstraint(
            "antecedencia_minima_min >= 0",
            name="ck_configuracoes_agente_antecedencia_nao_negativa",
        ),
    )


def downgrade() -> None:
    op.drop_table("configuracoes_agente")
