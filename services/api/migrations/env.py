import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from clinica_core.config import ConfigPostgres
from clinica_core.db import criar_engine
from clinica_core.modelos import Base

if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)


def _executar(conexao: Connection) -> None:
    context.configure(connection=conexao, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _executar_online() -> None:
    engine = criar_engine(ConfigPostgres())
    async with engine.connect() as conexao:
        await conexao.run_sync(_executar)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=ConfigPostgres().url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_executar_online())
