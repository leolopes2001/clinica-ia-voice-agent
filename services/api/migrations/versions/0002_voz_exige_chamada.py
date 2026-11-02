"""voz exige chamada

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        op.f("ck_agendamentos_voz_exige_chamada"),
        "agendamentos",
        "origem <> 'voz' OR chamada_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_agendamentos_voz_exige_chamada"), "agendamentos", type_="check")
