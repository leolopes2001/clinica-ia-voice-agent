from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import SecretStr

from clinica_core.config import ConfigEmbeddings
from clinica_core.embeddings import CacheDePerguntas, GeminiEmbeddings


class ModelosFalsos:
    def __init__(self) -> None:
        self.chamadas: list[dict[str, Any]] = []

    async def embed_content(self, *, model: str, contents: list[str], config: Any) -> Any:
        self.chamadas.append({"model": model, "contents": contents, "config": config})

        class Vetor:
            values = [0.5] * 768

        class Resposta:
            embeddings = [Vetor() for _ in contents]

        return Resposta()


def _gerador(monkeypatch: pytest.MonkeyPatch) -> tuple[GeminiEmbeddings, ModelosFalsos]:
    gerador = GeminiEmbeddings(
        ConfigEmbeddings(google_api_key=SecretStr("x"), embedding_modelo="m")
    )
    modelos = ModelosFalsos()

    class Aio:
        models = modelos

    class Cliente:
        aio = Aio()

    monkeypatch.setattr(gerador, "_obter_cliente", lambda: Cliente())
    return gerador, modelos


async def test_documentos_em_lotes_de_100(monkeypatch: pytest.MonkeyPatch) -> None:
    gerador, modelos = _gerador(monkeypatch)
    vetores = await gerador.documentos([f"t{i}" for i in range(250)])
    assert len(vetores) == 250
    assert [len(c["contents"]) for c in modelos.chamadas] == [100, 100, 50]
    configuracao = modelos.chamadas[0]["config"]
    assert configuracao.task_type == "RETRIEVAL_DOCUMENT"
    assert configuracao.output_dimensionality == 768


async def test_pergunta_usa_o_tipo_de_busca(monkeypatch: pytest.MonkeyPatch) -> None:
    gerador, modelos = _gerador(monkeypatch)
    assert len(await gerador.pergunta("oi")) == 768
    assert modelos.chamadas[0]["config"].task_type == "RETRIEVAL_QUERY"


async def test_resposta_incompleta_e_erro(monkeypatch: pytest.MonkeyPatch) -> None:
    gerador, modelos = _gerador(monkeypatch)

    async def incompleta(**_: Any) -> Any:
        class Resposta:
            embeddings: list[Any] = []

        return Resposta()

    monkeypatch.setattr(modelos, "embed_content", incompleta)
    with pytest.raises(RuntimeError, match="Esperava 1"):
        await gerador.pergunta("oi")


@pytest.mark.parametrize("chave", [None, SecretStr("")])
async def test_sem_chave_so_falha_no_uso(chave: SecretStr | None) -> None:
    gerador = GeminiEmbeddings(ConfigEmbeddings(google_api_key=chave))
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        await gerador.pergunta("oi")


class GeradorContado:
    def __init__(self) -> None:
        self.perguntas: list[str] = []
        self.falhar = False

    async def documentos(self, textos: Sequence[str]) -> list[list[float]]:
        return [[0.0] for _ in textos]

    async def pergunta(self, texto: str) -> list[float]:
        if self.falhar:
            raise RuntimeError("fora do ar")
        self.perguntas.append(texto)
        return [float(len(self.perguntas))]


async def test_cache_nao_repete_a_pergunta_nem_por_caixa_ou_espacos() -> None:
    gerador = GeradorContado()
    cache = CacheDePerguntas(gerador, 10)
    primeira = await cache.pergunta("Quanto custa a limpeza?")
    assert await cache.pergunta("  quanto   custa a LIMPEZA? ") == primeira
    assert gerador.perguntas == ["Quanto custa a limpeza?"]


async def test_cache_descarta_a_menos_usada() -> None:
    gerador = GeradorContado()
    cache = CacheDePerguntas(gerador, 2)
    await cache.pergunta("a")
    await cache.pergunta("b")
    await cache.pergunta("a")
    await cache.pergunta("c")
    await cache.pergunta("a")
    assert gerador.perguntas == ["a", "b", "c"]
    await cache.pergunta("b")
    assert gerador.perguntas == ["a", "b", "c", "b"]


async def test_cache_nao_guarda_falha() -> None:
    gerador = GeradorContado()
    cache = CacheDePerguntas(gerador, 2)
    gerador.falhar = True
    with pytest.raises(RuntimeError):
        await cache.pergunta("a")
    gerador.falhar = False
    assert await cache.pergunta("a") == [1.0]


async def test_cache_nao_guarda_o_texto_da_pergunta() -> None:
    cache = CacheDePerguntas(GeradorContado(), 2)
    await cache.pergunta("estou com dor de dente")
    assert all(isinstance(chave, bytes) and b"dor" not in chave for chave in cache._vetores)


async def test_cache_nao_interfere_nos_documentos() -> None:
    assert await CacheDePerguntas(GeradorContado(), 2).documentos(["a", "b"]) == [[0.0], [0.0]]
