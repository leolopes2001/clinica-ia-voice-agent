from collections.abc import Sequence
from typing import Protocol

from google import genai
from google.genai import types

from clinica_core.config import ConfigEmbeddings
from clinica_core.modelos import DIMENSAO_EMBEDDING

_LOTE_MAXIMO = 100


class GeradorEmbeddings(Protocol):
    async def documentos(self, textos: Sequence[str]) -> list[list[float]]: ...

    async def pergunta(self, texto: str) -> list[float]: ...


class GeminiEmbeddings:
    """O cliente nasce no primeiro uso: sem a chave, só a busca e a indexação falham."""

    def __init__(
        self, config: ConfigEmbeddings, tentativas: int = 5, limite_ms: int = 30_000
    ) -> None:
        self._config = config
        self._opcoes = types.HttpOptions(
            timeout=limite_ms, retry_options=types.HttpRetryOptions(attempts=tentativas)
        )
        self._cliente: genai.Client | None = None

    def _obter_cliente(self) -> genai.Client:
        if self._cliente is None:
            chave = self._config.google_api_key
            if chave is None or not chave.get_secret_value():
                raise RuntimeError("GOOGLE_API_KEY não configurada")
            self._cliente = genai.Client(
                api_key=chave.get_secret_value(), http_options=self._opcoes
            )
        return self._cliente

    async def _gerar(self, textos: Sequence[str], tipo: str) -> list[list[float]]:
        resposta = await self._obter_cliente().aio.models.embed_content(
            model=self._config.embedding_modelo,
            contents=list(textos),
            config=types.EmbedContentConfig(
                task_type=tipo, output_dimensionality=DIMENSAO_EMBEDDING
            ),
        )
        vetores = [e.values for e in resposta.embeddings or [] if e.values is not None]
        if len(vetores) != len(textos):
            raise RuntimeError(f"Esperava {len(textos)} embeddings e recebeu {len(vetores)}")
        return vetores

    async def documentos(self, textos: Sequence[str]) -> list[list[float]]:
        vetores: list[list[float]] = []
        for inicio in range(0, len(textos), _LOTE_MAXIMO):
            vetores += await self._gerar(
                textos[inicio : inicio + _LOTE_MAXIMO], "RETRIEVAL_DOCUMENT"
            )
        return vetores

    async def pergunta(self, texto: str) -> list[float]:
        return (await self._gerar([texto], "RETRIEVAL_QUERY"))[0]
