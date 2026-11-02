from typing import Any

import pytest
from pydantic import SecretStr

from clinica_core.config import ConfigEmbeddings
from clinica_core.embeddings import GeminiEmbeddings


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
