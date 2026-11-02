import uvicorn
from pydantic_settings import BaseSettings, SettingsConfigDict

from clinica_core.config import ConfigEmbeddings, ConfigPostgres
from clinica_core.db import criar_engine
from clinica_core.embeddings import CacheDePerguntas, GeminiEmbeddings
from clinica_mcp_conhecimento.servidor import criar_app

_TENTATIVAS_EMBEDDING = 2
_LIMITE_EMBEDDING_MS = 2_000
_PERGUNTAS_EM_CACHE = 1024


class ConfigMcpConhecimento(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MCP_CONHECIMENTO_", env_file=".env", extra="ignore"
    )

    porta: int = 8000


def main() -> None:
    # O paciente está esperando na linha: falha rápido e cai na busca só por texto.
    gerador = CacheDePerguntas(
        GeminiEmbeddings(
            ConfigEmbeddings(), tentativas=_TENTATIVAS_EMBEDDING, limite_ms=_LIMITE_EMBEDDING_MS
        ),
        _PERGUNTAS_EM_CACHE,
    )
    app = criar_app(criar_engine(ConfigPostgres()), gerador)
    uvicorn.run(app, host="0.0.0.0", port=ConfigMcpConhecimento().porta)


if __name__ == "__main__":
    main()
