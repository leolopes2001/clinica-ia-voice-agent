import pytest
from sqlalchemy import text

from clinica_core.config import ConfigPostgres
from clinica_core.db import criar_engine, criar_fabrica_sessoes


async def test_select_1_no_postgres() -> None:
    engine = criar_engine(ConfigPostgres())
    try:
        async with criar_fabrica_sessoes(engine)() as sessao:
            assert await sessao.scalar(text("SELECT 1")) == 1
    except OSError:
        pytest.skip("postgres indisponível: rode `docker compose up -d --wait`")
    finally:
        await engine.dispose()
