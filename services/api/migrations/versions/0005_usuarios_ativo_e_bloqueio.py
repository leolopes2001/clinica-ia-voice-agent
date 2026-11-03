"""usuário desativado e bloqueio por tentativas de login

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usuarios", sa.Column("ativo", sa.Boolean(), server_default=sa.text("true")))
    op.alter_column("usuarios", "ativo", nullable=False)
    op.add_column(
        "usuarios", sa.Column("falhas_de_login", sa.Integer(), server_default=sa.text("0"))
    )
    op.alter_column("usuarios", "falhas_de_login", nullable=False)
    op.add_column("usuarios", sa.Column("bloqueado_ate", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("usuarios", "bloqueado_ate")
    op.drop_column("usuarios", "falhas_de_login")
    op.drop_column("usuarios", "ativo")
