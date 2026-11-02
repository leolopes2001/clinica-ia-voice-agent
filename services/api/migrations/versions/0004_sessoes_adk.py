"""usuário e schema das sessões do ADK

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

from clinica_core.config import ConfigSessoesAdk

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_PAPEL = "adk_sessoes"


def upgrade() -> None:
    conexao = op.get_bind()
    # O papel vale para todo o servidor, e o schema, só para este banco.
    existe = conexao.scalar(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :p"), {"p": _PAPEL})
    if not existe:
        senha = ConfigSessoesAdk().senha.get_secret_value()
        comando = conexao.scalar(
            sa.text(
                "SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', "
                "CAST(:p AS text), CAST(:s AS text))"
            ),
            {"p": _PAPEL, "s": senha},
        )
        conexao.exec_driver_sql(comando)
    op.execute(f"CREATE SCHEMA {_PAPEL} AUTHORIZATION {_PAPEL}")
    op.execute(f"ALTER ROLE {_PAPEL} SET search_path = {_PAPEL}")


def downgrade() -> None:
    # O papel fica: outro banco do mesmo servidor pode estar usando.
    op.execute(f"DROP SCHEMA {_PAPEL} CASCADE")
