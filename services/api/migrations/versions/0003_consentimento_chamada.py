"""consentimento na chamada

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for coluna in ("consentimento_em", "consentimento_recusado_em"):
        op.add_column("chamadas", sa.Column(coluna, sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    for coluna in ("consentimento_recusado_em", "consentimento_em"):
        op.drop_column("chamadas", coluna)
