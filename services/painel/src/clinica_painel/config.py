from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigPainel(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAINEL_", env_file=".env", extra="ignore")

    api_url: str = "http://localhost:8000"
    mcp_clinica_url: str = "http://localhost:8101"
    mcp_conhecimento_url: str = "http://localhost:8102"
    livekit_url: str = "http://localhost:7880"
    agente_url: str = "http://localhost:8081"
    rabbit_gerencia_url: str = "http://localhost:15672"
    backups_dir: Path = Path("/backups")
    porta: int = 8080
    # Padrão de desenvolvimento: troque antes de usar em produção (T6.5).
    segredo_sessao: SecretStr = SecretStr("segredo-de-sessao-de-desenvolvimento-troque")
    # O navegador só devolve o cookie por HTTPS ou em localhost. Sem HTTPS na rede local, use false.
    cookie_seguro: bool = True
    horas_de_sessao: int = 8
    conhecimento_dir: Path = Path("/dados/conhecimento")
