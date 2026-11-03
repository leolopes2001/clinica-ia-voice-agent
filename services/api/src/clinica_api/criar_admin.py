"""Cria o primeiro administrador: `docker compose exec api python -m clinica_api.criar_admin`."""

import argparse
import asyncio
import getpass

from sqlalchemy import select

from clinica_api.autenticacao import gerar_hash
from clinica_api.esquemas_painel import UsuarioCriar
from clinica_core.config import ConfigPostgres
from clinica_core.db import criar_engine, criar_fabrica_sessoes
from clinica_core.modelos import Clinica, Usuario


async def criar(email: str, senha: str, config: ConfigPostgres | None = None) -> str:
    dados = UsuarioCriar(email=email, senha=senha, perfil="admin")
    engine = criar_engine(config or ConfigPostgres())
    try:
        async with criar_fabrica_sessoes(engine)() as sessao:
            clinicas = (await sessao.scalars(select(Clinica.id))).all()
            if len(clinicas) != 1:
                raise SystemExit(f"Esperava 1 clínica e há {len(clinicas)}.")
            if await sessao.scalar(select(Usuario.id).where(Usuario.email == dados.email)):
                raise SystemExit("Este e-mail já está em uso.")
            sessao.add(
                Usuario(
                    clinica_id=clinicas[0],
                    email=dados.email,
                    senha_hash=gerar_hash(dados.senha),
                    perfil=dados.perfil,
                )
            )
            await sessao.commit()
    finally:
        await engine.dispose()
    return dados.email


if __name__ == "__main__":
    argumentos = argparse.ArgumentParser(description=__doc__)
    argumentos.add_argument("--email", required=True)
    email_novo = asyncio.run(
        criar(argumentos.parse_args().email, getpass.getpass("Senha (10 ou mais caracteres): "))
    )
    print(f"Administrador {email_novo} criado.")
