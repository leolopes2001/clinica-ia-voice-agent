from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from clinica_api.app import criar_app as criar_api
from clinica_api.autenticacao import gerar_hash
from clinica_core.config import ConfigAgenda, ConfigPostgres, ConfigTokens
from clinica_core.modelos import Clinica, Usuario
from clinica_painel.app import criar_app as criar_painel
from clinica_painel.config import ConfigPainel
from clinica_painel.eventos import Distribuidor

SENHA = "senha-de-teste-123"


class PublicadorFalso:
    def __init__(self) -> None:
        self.eventos_publicados: list[Any] = []

    async def abrir(self) -> None:
        pass

    async def fechar(self) -> None:
        pass

    async def hold_criado(self, hold_id: UUID, duracao: Any) -> None:
        pass

    async def eventos(self, eventos: Any) -> None:
        self.eventos_publicados.extend(eventos)


@dataclass
class Ambiente:
    api: FastAPI
    painel: FastAPI
    clinica_id: UUID
    admin_id: UUID
    recepcao_id: UUID
    eventos: Distribuidor
    publicador: PublicadorFalso
    busca_pedidos: list[httpx.Request]
    # O que a busca do mcp-conhecimento falsa devolve.
    busca_trechos: list[dict[str, str]]


@pytest.fixture
async def ambiente(banco: str) -> AsyncIterator[Ambiente]:
    publicador = PublicadorFalso()
    api = criar_api(ConfigPostgres(db=banco), ConfigAgenda(antecedencia_minima_min=0), publicador)
    async with api.state.fabrica_sessoes() as sessao:
        clinica = Clinica(
            nome="Clínica", telefone="+5511", endereco="R", politica_cancelamento_horas=24
        )
        sessao.add(clinica)
        await sessao.flush()
        hash_ = gerar_hash(SENHA)
        admin = Usuario(
            clinica_id=clinica.id,
            email=f"admin-{clinica.id}@teste.test",
            senha_hash=hash_,
            perfil="admin",
        )
        recepcao = Usuario(
            clinica_id=clinica.id,
            email=f"recepcao-{clinica.id}@teste.test",
            senha_hash=hash_,
            perfil="recepcao",
        )
        sessao.add_all([admin, recepcao])
        await sessao.commit()
        dados = (clinica.id, admin.id, recepcao.id)
    pedidos: list[httpx.Request] = []
    trechos: list[dict[str, str]] = []

    def responder_busca(pedido: httpx.Request) -> httpx.Response:
        pedidos.append(pedido)
        return httpx.Response(200, json={"trechos": trechos})

    cliente_api = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://api",
        headers=ConfigTokens().cabecalho_painel,
    )
    cliente_busca = httpx.AsyncClient(
        transport=httpx.MockTransport(responder_busca), base_url="http://busca"
    )
    eventos = Distribuidor()
    painel = criar_painel(cliente_api, cliente_busca, ConfigPainel(cookie_seguro=False), eventos)
    yield Ambiente(api, painel, *dados, eventos, publicador, pedidos, trechos)
    await cliente_api.aclose()
    await cliente_busca.aclose()
    await api.state.engine.dispose()


def _navegador(ambiente: Ambiente) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ambiente.painel), base_url="http://painel"
    )


Entrar = Callable[[str], Awaitable[httpx.AsyncClient]]


@pytest.fixture
async def entrar(ambiente: Ambiente) -> AsyncIterator[Entrar]:
    abertos: list[httpx.AsyncClient] = []

    async def _entrar(perfil: str) -> httpx.AsyncClient:
        cliente = _navegador(ambiente)
        abertos.append(cliente)
        resposta = await cliente.post(
            "/login",
            data={"email": f"{perfil}-{ambiente.clinica_id}@teste.test", "senha": SENHA},
        )
        assert resposta.status_code == 303
        return cliente

    yield _entrar
    for cliente in abertos:
        await cliente.aclose()


@pytest.fixture
async def anonimo(ambiente: Ambiente) -> AsyncIterator[httpx.AsyncClient]:
    async with _navegador(ambiente) as cliente:
        yield cliente


@pytest.fixture
async def admin(entrar: Entrar) -> httpx.AsyncClient:
    return await entrar("admin")


@pytest.fixture
async def recepcao(entrar: Entrar) -> httpx.AsyncClient:
    return await entrar("recepcao")
