from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from clinica_core.config import ConfigPostgres, ConfigTokens
from clinica_core.db import criar_engine, criar_fabrica_sessoes
from clinica_core.modelos import (
    DIMENSAO_EMBEDDING,
    Chamada,
    Clinica,
    ConhecimentoDocumento,
    ConhecimentoTrecho,
)
from clinica_mcp_conhecimento.busca import (
    CANDIDATOS,
    MAXIMO_CARACTERES,
    buscar,
    combinar_rrf,
    recortar,
)
from clinica_mcp_conhecimento.servidor import criar_app, criar_servidor

Fabrica = async_sessionmaker[AsyncSession]


def _base(eixo: int) -> list[float]:
    vetor = [0.0] * DIMENSAO_EMBEDDING
    vetor[eixo] = 1.0
    return vetor


class GeradorFalso:
    """Devolve o vetor combinado com a pergunta, para o teste controlar o ranking por vetor."""

    def __init__(self, vetor: list[float] | None = None, erro: Exception | None = None) -> None:
        self.vetor = vetor or _base(0)
        self.erro = erro

    async def documentos(self, textos: Sequence[str]) -> list[list[float]]:
        return [self.vetor for _ in textos]

    async def pergunta(self, texto: str) -> list[float]:
        if self.erro is not None:
            raise self.erro
        return self.vetor


@pytest.fixture
async def engine(banco: str) -> AsyncIterator[AsyncEngine]:
    engine = criar_engine(ConfigPostgres(db=banco))
    yield engine
    await engine.dispose()


@pytest.fixture
def fabrica(engine: AsyncEngine) -> Fabrica:
    return criar_fabrica_sessoes(engine)


async def _clinica(fabrica: Fabrica) -> UUID:
    async with fabrica() as sessao:
        clinica = Clinica(nome="C", telefone="+5511", endereco="R", politica_cancelamento_horas=24)
        sessao.add(clinica)
        await sessao.commit()
        return clinica.id


async def _documento(
    fabrica: Fabrica,
    clinica_id: UUID,
    trechos: list[tuple[str, list[float]]],
    titulo: str = "Preços",
    ativo: bool = True,
) -> None:
    async with fabrica() as sessao:
        documento = ConhecimentoDocumento(
            clinica_id=clinica_id, titulo=titulo, status="pronto", ativo=ativo
        )
        sessao.add(documento)
        await sessao.flush()
        sessao.add_all(
            ConhecimentoTrecho(
                clinica_id=clinica_id,
                documento_id=documento.id,
                ordem=ordem,
                texto=texto_trecho,
                embedding=vetor,
            )
            for ordem, (texto_trecho, vetor) in enumerate(trechos)
        )
        await sessao.commit()


async def _chamada(fabrica: Fabrica, clinica_id: UUID) -> str:
    async with fabrica() as sessao:
        chamada = Chamada(clinica_id=clinica_id, telefone="+5511900000001", sala="call")
        sessao.add(chamada)
        await sessao.commit()
        return str(chamada.id)


# ---- RRF


def test_rrf_favorece_quem_aparece_nas_duas_listas() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    assert combinar_rrf([a, b], [c, b])[0] == b


def test_rrf_segue_a_formula_com_k_60() -> None:
    a, b = uuid4(), uuid4()
    # a: 1/61 + 1/70 = 0,03068; b: 1/62 + 1/62 = 0,03226.
    outros = [uuid4() for _ in range(8)]
    assert combinar_rrf([a, b], [b, *outros, a]) == [b, a, *outros]


def test_rrf_com_uma_lista_vazia_mantem_a_ordem() -> None:
    a, b = uuid4(), uuid4()
    assert combinar_rrf([], [a, b]) == [a, b]
    assert combinar_rrf([], []) == []


# ---- Recorte em 800 caracteres


def test_texto_curto_nao_muda() -> None:
    assert recortar("Título\nCusta 150.", "preço") == "Título\nCusta 150."


def test_recorte_mantem_o_titulo_e_a_frase_da_pergunta() -> None:
    enchimento = " ".join(f"Frase sem relação número {i} sobre outro assunto." for i in range(60))
    texto = f"Preços > Canal\n{enchimento} O canal custa novecentos reais. {enchimento}"
    recortado = recortar(texto, "quanto custa o canal?")
    assert len(recortado) <= MAXIMO_CARACTERES
    assert recortado.startswith("Preços > Canal\n")
    assert "O canal custa novecentos reais." in recortado


def test_recorte_de_uma_frase_gigante_respeita_o_limite() -> None:
    assert len(recortar("palavra " * 500, "palavra")) <= MAXIMO_CARACTERES


# ---- Busca híbrida


async def test_busca_combina_vetor_e_texto(fabrica: Fabrica) -> None:
    clinica_id = await _clinica(fabrica)
    await _documento(
        fabrica,
        clinica_id,
        [
            ("Só o vetor encontra este.", _base(0)),
            ("Clareamento custa 800 reais.", _base(1)),
            ("Irrelevante.", _base(2)),
            ("Muito irrelevante.", _base(3)),
        ],
    )
    async with fabrica() as sessao:
        trechos = await buscar(sessao, GeradorFalso(_base(0)), clinica_id, "preço do clareamento")
    textos = [t.texto for t in trechos]
    assert len(textos) == 3
    assert {"Só o vetor encontra este.", "Clareamento custa 800 reais."} <= set(textos)
    assert all(t.fonte == "Preços" for t in trechos)


async def test_pergunta_casa_com_qualquer_palavra_no_texto(fabrica: Fabrica) -> None:
    clinica_id = await _clinica(fabrica)
    await _documento(fabrica, clinica_id, [("Aceitamos o convênio Amil.", _base(0))])
    async with fabrica() as sessao:
        # "dental" e "hoje" não estão no trecho. Com E entre as palavras, não casaria.
        trechos = await buscar(
            sessao, GeradorFalso(erro=RuntimeError()), clinica_id, "convênio dental hoje"
        )
    assert [t.texto for t in trechos] == ["Aceitamos o convênio Amil."]


async def test_embedding_fora_do_ar_cai_na_busca_por_texto(fabrica: Fabrica) -> None:
    clinica_id = await _clinica(fabrica)
    await _documento(
        fabrica, clinica_id, [("Estacionamento gratuito.", _base(0)), ("Outro.", _base(1))]
    )
    gerador = GeradorFalso(erro=RuntimeError("fora do ar"))
    async with fabrica() as sessao:
        trechos = await buscar(sessao, gerador, clinica_id, "tem estacionamento?")
    assert [t.texto for t in trechos] == ["Estacionamento gratuito."]


async def test_sem_nada_encontrado_devolve_vazio(fabrica: Fabrica) -> None:
    clinica_id = await _clinica(fabrica)
    async with fabrica() as sessao:
        assert await buscar(sessao, GeradorFalso(), clinica_id, "qualquer coisa") == []


async def test_pergunta_so_com_palavras_vazias_nao_quebra(fabrica: Fabrica) -> None:
    clinica_id = await _clinica(fabrica)
    await _documento(fabrica, clinica_id, [("Texto.", _base(0))])
    async with fabrica() as sessao:
        trechos = await buscar(sessao, GeradorFalso(erro=RuntimeError()), clinica_id, "de que o")
    assert trechos == []


async def test_busca_filtra_clinica(fabrica: Fabrica) -> None:
    minha, outra = await _clinica(fabrica), await _clinica(fabrica)
    await _documento(fabrica, outra, [("Preço da outra clínica.", _base(0))])
    await _documento(fabrica, minha, [("Preço da minha clínica.", _base(1))])
    async with fabrica() as sessao:
        trechos = await buscar(sessao, GeradorFalso(_base(0)), minha, "preço")
    assert [t.texto for t in trechos] == ["Preço da minha clínica."]


async def test_busca_ignora_documento_inativo(fabrica: Fabrica) -> None:
    clinica_id = await _clinica(fabrica)
    await _documento(fabrica, clinica_id, [("Tabela antiga.", _base(0))], ativo=False)
    await _documento(fabrica, clinica_id, [("Tabela nova.", _base(1))])
    async with fabrica() as sessao:
        trechos = await buscar(sessao, GeradorFalso(_base(0)), clinica_id, "tabela")
    assert [t.texto for t in trechos] == ["Tabela nova."]


async def test_k_nunca_passa_de_3(fabrica: Fabrica) -> None:
    clinica_id = await _clinica(fabrica)
    await _documento(fabrica, clinica_id, [(f"Trecho {i}.", _base(i)) for i in range(CANDIDATOS)])
    async with fabrica() as sessao:
        assert len(await buscar(sessao, GeradorFalso(), clinica_id, "trecho", k=50)) == 3
        assert len(await buscar(sessao, GeradorFalso(), clinica_id, "trecho", k=1)) == 1
        assert len(await buscar(sessao, GeradorFalso(), clinica_id, "trecho", k=0)) == 1


# ---- Servidor MCP


@asynccontextmanager
async def _cliente(
    engine: AsyncEngine, gerador: GeradorFalso, cabecalhos: dict[str, str]
) -> AsyncIterator[Client]:
    app = criar_app(engine, gerador)
    async with app.router.lifespan_context(app):
        transporte = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transporte, base_url="http://mcp", headers=cabecalhos
        ) as http:
            async with Client(
                streamable_http_client("http://mcp/mcp", http_client=http)
            ) as cliente:
                yield cliente


async def _buscar(cliente: Client, **argumentos: Any) -> dict[str, Any]:
    resultado = await cliente.call_tool("buscar_conhecimento", argumentos)
    assert not resultado.is_error, resultado.content
    corpo: dict[str, Any] = resultado.structured_content or {}
    return corpo


async def test_expoe_so_a_busca_sem_clinica_nos_parametros(engine: AsyncEngine) -> None:
    ferramentas = await criar_servidor(engine, GeradorFalso()).list_tools()
    assert [f.name for f in ferramentas] == ["buscar_conhecimento"]
    assert set(ferramentas[0].input_schema["properties"]) == {"pergunta", "k"}


async def test_clinica_vem_da_chamada(engine: AsyncEngine, fabrica: Fabrica) -> None:
    minha, outra = await _clinica(fabrica), await _clinica(fabrica)
    await _documento(fabrica, minha, [("Da minha clínica.", _base(0))], titulo="Sobre")
    await _documento(fabrica, outra, [("Da outra clínica.", _base(0))], titulo="Sobre")
    chamada_id = await _chamada(fabrica, minha)
    cabecalhos = {"X-Chamada-Id": chamada_id, "X-Clinica-Id": str(outra)}
    async with _cliente(engine, GeradorFalso(), cabecalhos) as cliente:
        corpo = await _buscar(cliente, pergunta="clínica")
    assert corpo == {"ok": True, "trechos": [{"texto": "Da minha clínica.", "fonte": "Sobre"}]}


async def test_trecho_devolvido_tem_ate_800_caracteres(
    engine: AsyncEngine, fabrica: Fabrica
) -> None:
    clinica_id = await _clinica(fabrica)
    longo = "Preços\n" + " ".join(f"Frase {i} qualquer." for i in range(200))
    await _documento(fabrica, clinica_id, [(longo, _base(0))])
    chamada_id = await _chamada(fabrica, clinica_id)
    async with _cliente(engine, GeradorFalso(), {"X-Chamada-Id": chamada_id}) as cliente:
        corpo = await _buscar(cliente, pergunta="frase")
    assert len(corpo["trechos"][0]["texto"]) <= 800


async def test_chamada_inexistente(engine: AsyncEngine) -> None:
    async with _cliente(engine, GeradorFalso(), {"X-Chamada-Id": str(uuid4())}) as cliente:
        corpo = await _buscar(cliente, pergunta="preço")
    assert corpo == {"ok": False, "motivo": "chamada_nao_encontrada", "trechos": []}


@pytest.mark.parametrize("cabecalhos", [{}, {"X-Chamada-Id": "nao-e-uuid"}])
async def test_header_ausente_ou_invalido_e_recusado(
    engine: AsyncEngine, cabecalhos: dict[str, str]
) -> None:
    async with _cliente(engine, GeradorFalso(), cabecalhos) as cliente:
        resultado = await cliente.call_tool("buscar_conhecimento", {"pergunta": "preço"})
    assert resultado.is_error


async def test_saude(engine: AsyncEngine) -> None:
    transporte = httpx.ASGITransport(app=criar_app(engine, GeradorFalso()))
    async with httpx.AsyncClient(transport=transporte, base_url="http://mcp") as http:
        assert (await http.get("/saude")).json() == {"status": "ok"}


async def test_conexao_do_servidor_e_somente_leitura(engine: AsyncEngine) -> None:
    somente_leitura = engine.execution_options(postgresql_readonly=True)
    async with async_sessionmaker(somente_leitura)() as sessao:
        await sessao.execute(select(text("1")))
        with pytest.raises(DBAPIError, match="read-only"):
            await sessao.execute(text("CREATE TEMP TABLE x (a int)"))


async def test_testar_pergunta_exige_o_token_do_painel(engine: AsyncEngine) -> None:
    transporte = httpx.ASGITransport(app=criar_app(engine, GeradorFalso()))
    async with httpx.AsyncClient(transport=transporte, base_url="http://mcp") as cliente:
        corpo = {"clinica_id": str(uuid4()), "pergunta": "oi"}
        assert (await cliente.post("/testar-pergunta", json=corpo)).status_code == 401
        errado = await cliente.post(
            "/testar-pergunta", json=corpo, headers={"Authorization": "Bearer errado"}
        )
        assert errado.status_code == 401
        servicos = await cliente.post(
            "/testar-pergunta", json=corpo, headers=ConfigTokens().cabecalho_servicos
        )
        assert servicos.status_code == 401
        invalido = await cliente.post(
            "/testar-pergunta", json={"pergunta": "oi"}, headers=ConfigTokens().cabecalho_painel
        )
        assert invalido.status_code == 422
        vazio = await cliente.post(
            "/testar-pergunta", json=corpo, headers=ConfigTokens().cabecalho_painel
        )
        assert (vazio.status_code, vazio.json()) == (200, {"trechos": []})
