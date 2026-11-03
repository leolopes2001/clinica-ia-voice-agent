from pathlib import Path
from uuid import UUID

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigAgente(BaseSettings):
    # env_ignore_empty: o Compose repassa as opcionais vazias.
    model_config = SettingsConfigDict(
        env_prefix="AGENTE_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    modelo_voz: str = "gemini-3.1-flash-live-preview"
    api_url: str = "http://localhost:8000"
    mcp_clinica_url: str = "http://localhost:8101/mcp"
    mcp_conhecimento_url: str = "http://localhost:8102/mcp"
    # Vazio: usa a única clínica da instalação.
    clinica_id: UUID | None = None
    permitir_telefone_de_teste: bool = False
    # Silêncio até o modelo considerar que a pessoa terminou. Vazio: o padrão do Gemini.
    silencio_fim_de_fala_ms: int | None = None
    # WAV mono de 16 bits, feito por scripts/gerar_frase_espera.py. Sem o arquivo, não há frase.
    frase_espera_arquivo: Path = Path("infra/audio/frase_espera.wav")


class ConfigLiveKit(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LIVEKIT_", env_file=".env", extra="ignore")

    url: str = "ws://localhost:7880"
    url_navegador: str = "ws://localhost:7880"
    api_key: str = "clinica-dev"
    api_secret: SecretStr = SecretStr("segredo-de-desenvolvimento-troque-antes-de-usar")
