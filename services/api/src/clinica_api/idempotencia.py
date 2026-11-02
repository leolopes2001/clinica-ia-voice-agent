from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_core.modelos import Idempotencia

VALIDADE = timedelta(hours=24)


async def buscar(sessao: AsyncSession, chave: str) -> dict[str, Any] | None:
    return await sessao.scalar(
        select(Idempotencia.resposta_json).where(
            Idempotencia.chave == chave, Idempotencia.criado_em > func.now() - VALIDADE
        )
    )


async def guardar(sessao: AsyncSession, chave: str, resposta: dict[str, Any]) -> None:
    # A chave pode já existir: resposta vencida ou hold que deixou de valer.
    comando = insert(Idempotencia).values(chave=chave, resposta_json=resposta)
    await sessao.execute(
        comando.on_conflict_do_update(
            index_elements=[Idempotencia.chave],
            set_={"resposta_json": comando.excluded.resposta_json, "criado_em": func.now()},
        )
    )
