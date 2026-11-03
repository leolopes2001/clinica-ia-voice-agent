"""telefone da chamada e dados do pedido de retorno podem ser apagados

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("chamadas", "telefone", nullable=True)
    op.alter_column("pedidos_retorno", "telefone", nullable=True)
    op.alter_column("pedidos_retorno", "motivo", nullable=True)


def downgrade() -> None:
    # Só desce se nenhuma linha tiver sido anonimizada: o dado apagado não volta.
    op.alter_column("pedidos_retorno", "motivo", nullable=False)
    op.alter_column("pedidos_retorno", "telefone", nullable=False)
    op.alter_column("chamadas", "telefone", nullable=False)
