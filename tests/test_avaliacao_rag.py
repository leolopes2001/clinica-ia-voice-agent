import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from statistics import quantiles
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from clinica_core.config import ConfigEmbeddings, ConfigPostgres
from clinica_core.db import criar_engine, criar_fabrica_sessoes
from clinica_core.embeddings import GeminiEmbeddings
from clinica_core.modelos import Clinica, ConhecimentoDocumento
from clinica_mcp_conhecimento.busca import buscar
from clinica_worker.conhecimento import indexar_documento

RAIZ = Path(__file__).resolve().parent.parent
BASE = RAIZ / "exemplos" / "conhecimento"
PERGUNTAS: list[dict[str, str]] = json.loads(
    (RAIZ / "tests" / "avaliacao" / "perguntas.json").read_text(encoding="utf-8")
)
ACERTO_MINIMO = 0.9
Fabrica = async_sessionmaker[AsyncSession]


def test_conjunto_tem_30_perguntas_e_cobre_todos_os_documentos() -> None:
    assert len(PERGUNTAS) == 30
    assert {p["fonte"] for p in PERGUNTAS} == {arquivo.name for arquivo in BASE.glob("*.md")}


@pytest.mark.parametrize("pergunta", PERGUNTAS, ids=lambda p: p["pergunta"])
def test_resposta_esperada_esta_no_documento_certo(pergunta: dict[str, str]) -> None:
    assert pergunta["contem"] in (BASE / pergunta["fonte"]).read_text(encoding="utf-8")


@pytest.fixture
def gerador() -> GeminiEmbeddings:
    config = ConfigEmbeddings()
    if config.google_api_key is None or not config.google_api_key.get_secret_value():
        pytest.skip("Usa os embeddings reais e gasta crédito: rode com RODAR_TESTES_GEMINI=1")
    return GeminiEmbeddings(config)


@pytest.fixture
async def engine(banco: str) -> AsyncIterator[AsyncEngine]:
    engine = criar_engine(ConfigPostgres(db=banco))
    yield engine
    await engine.dispose()


async def _indexar_base(fabrica: Fabrica, gerador: GeminiEmbeddings) -> UUID:
    async with fabrica() as sessao:
        clinica = Clinica(
            nome="Clínica Exemplo Sorriso",
            telefone="+551130000000",
            endereco="Rua das Palmeiras Fictícias, 100",
            politica_cancelamento_horas=24,
        )
        sessao.add(clinica)
        await sessao.flush()
        documentos = [
            ConhecimentoDocumento(clinica_id=clinica.id, titulo=arquivo.name, arquivo=arquivo.name)
            for arquivo in sorted(BASE.glob("*.md"))
        ]
        sessao.add_all(documentos)
        await sessao.commit()
        clinica_id = clinica.id
        ids = [documento.id for documento in documentos]
    for documento_id in ids:
        await indexar_documento(fabrica, gerador, BASE, documento_id)
    return clinica_id


async def test_trecho_certo_no_top_3_em_pelo_menos_90_por_cento(
    engine: AsyncEngine, gerador: GeminiEmbeddings, capsys: pytest.CaptureFixture[str]
) -> None:
    fabrica = criar_fabrica_sessoes(engine)
    clinica_id = await _indexar_base(fabrica, gerador)

    erros: list[str] = []
    tempos: list[float] = []
    for item in PERGUNTAS:
        async with fabrica() as sessao:
            inicio = time.perf_counter()
            trechos = await buscar(sessao, gerador, clinica_id, item["pergunta"])
            tempos.append(time.perf_counter() - inicio)
        # O acerto exige o documento certo e a resposta dentro do texto devolvido ao agente.
        if not any(t.fonte == item["fonte"] and item["contem"] in t.texto for t in trechos):
            erros.append(f"{item['pergunta']} -> {[t.fonte for t in trechos]}")

    acerto = 1 - len(erros) / len(PERGUNTAS)
    p50, _, p95 = (quantiles(tempos, n=20)[i] for i in (9, 17, 18))
    with capsys.disabled():
        print(
            f"\nAcerto: {acerto:.0%}. Busca: mediana {p50 * 1000:.0f} ms, p95 {p95 * 1000:.0f} ms."
        )
        for erro in erros:
            print(f"  ERROU: {erro}")
    assert acerto >= ACERTO_MINIMO, erros
