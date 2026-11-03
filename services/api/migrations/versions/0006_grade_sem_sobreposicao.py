"""grade de trabalho sem sobreposição por dentista e por consultório

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# time não tem tipo de intervalo próprio: uma data fixa transforma o horário em timestamp.
_PERIODO = "tsrange(DATE '2000-01-01' + inicio, DATE '2000-01-01' + fim)"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE grade_trabalho ADD CONSTRAINT grade_sem_conflito_dentista "
        f"EXCLUDE USING gist (dentista_id WITH =, dia_semana WITH =, ({_PERIODO}) WITH &&)"
    )
    op.execute(
        f"ALTER TABLE grade_trabalho ADD CONSTRAINT grade_sem_conflito_consultorio "
        f"EXCLUDE USING gist (consultorio_id WITH =, dia_semana WITH =, ({_PERIODO}) WITH &&)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE grade_trabalho DROP CONSTRAINT grade_sem_conflito_consultorio")
    op.execute("ALTER TABLE grade_trabalho DROP CONSTRAINT grade_sem_conflito_dentista")
