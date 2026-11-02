from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def travar(sessao: AsyncSession, *chaves: object) -> None:
    """Toma locks até o fim da transação, sempre na mesma ordem para não causar deadlock."""
    for chave in sorted({str(c) for c in chaves}):
        await sessao.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(chave, 0))))
