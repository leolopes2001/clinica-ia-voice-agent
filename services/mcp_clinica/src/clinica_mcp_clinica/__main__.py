import httpx
import uvicorn
from pydantic_settings import BaseSettings, SettingsConfigDict

from clinica_core.config import ConfigTokens
from clinica_mcp_clinica.servidor import criar_app


class ConfigMcpClinica(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MCP_CLINICA_", env_file=".env", extra="ignore")

    api_url: str = "http://localhost:8000"
    porta: int = 8000


def main() -> None:
    config = ConfigMcpClinica()
    api = httpx.AsyncClient(
        base_url=config.api_url, timeout=5.0, headers=ConfigTokens().cabecalho_servicos
    )
    uvicorn.run(criar_app(api), host="0.0.0.0", port=config.porta)


if __name__ == "__main__":
    main()
