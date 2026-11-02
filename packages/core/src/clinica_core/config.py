from pathlib import Path
from urllib.parse import quote

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigPostgres(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POSTGRES_", env_file=".env", extra="ignore")

    host: str = "localhost"
    port: int = 5432
    user: str = "clinica"
    password: SecretStr = SecretStr("clinica")
    db: str = "clinica"

    @property
    def url(self) -> str:
        usuario = quote(self.user, safe="")
        senha = quote(self.password.get_secret_value(), safe="")
        return f"postgresql+asyncpg://{usuario}:{senha}@{self.host}:{self.port}/{self.db}"


class ConfigSessoesAdk(BaseSettings):
    """Usuário só das sessões do ADK: não enxerga as tabelas da clínica."""

    model_config = SettingsConfigDict(env_prefix="ADK_SESSOES_", env_file=".env", extra="ignore")

    host: str = "localhost"
    port: int = 5432
    usuario: str = "adk_sessoes"
    senha: SecretStr = SecretStr("adk_sessoes")
    db: str = "clinica"

    @property
    def url(self) -> str:
        usuario = quote(self.usuario, safe="")
        senha = quote(self.senha.get_secret_value(), safe="")
        return f"postgresql+asyncpg://{usuario}:{senha}@{self.host}:{self.port}/{self.db}"


class ConfigRabbit(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RABBITMQ_", env_file=".env", extra="ignore")

    host: str = "localhost"
    port: int = 5672
    user: str = "clinica"
    password: SecretStr = SecretStr("clinica")
    vhost: str = "/"

    @property
    def url(self) -> str:
        usuario = quote(self.user, safe="")
        senha = quote(self.password.get_secret_value(), safe="")
        vhost = quote(self.vhost, safe="")
        return f"amqp://{usuario}:{senha}@{self.host}:{self.port}/{vhost}"


class ConfigAgenda(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENDA_", env_file=".env", extra="ignore")

    antecedencia_minima_min: int = Field(default=120, ge=0)
    duracao_hold_min: int = Field(default=3, ge=1)


class ConfigRetencao(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RETENCAO_", env_file=".env", extra="ignore")

    transcricao_dias: int = Field(default=90, ge=1)


class ConfigEmbeddings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    google_api_key: SecretStr | None = None
    embedding_modelo: str = "gemini-embedding-001"


class ConfigConhecimento(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONHECIMENTO_", env_file=".env", extra="ignore")

    # Volume onde o painel salva os arquivos enviados.
    dir: Path = Path("/dados/conhecimento")
