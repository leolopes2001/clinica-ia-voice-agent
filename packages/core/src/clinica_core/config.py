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
