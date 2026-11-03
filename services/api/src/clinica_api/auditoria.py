from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from clinica_core.modelos import Auditoria


def registrar(
    sessao: AsyncSession,
    clinica_id: UUID,
    usuario_id: UUID | None,
    acao: str,
    entidade: str,
    antes: dict[str, Any] | None = None,
    depois: dict[str, Any] | None = None,
) -> None:
    """Entra na transação de quem chama: a ação e o registro dela gravam juntos."""
    sessao.add(
        Auditoria(
            clinica_id=clinica_id,
            usuario_id=usuario_id,
            acao=acao,
            entidade=entidade,
            antes=antes,
            depois=depois,
        )
    )
