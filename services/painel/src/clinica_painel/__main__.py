import httpx
import uvicorn

from clinica_core.config import ConfigRabbit, ConfigTokens
from clinica_core.logs import configurar_logs
from clinica_painel.app import criar_app
from clinica_painel.config import ConfigPainel
from clinica_painel.eventos import FonteDeEventosRabbit


def main() -> None:
    configurar_logs("painel")
    config = ConfigPainel()
    tokens = ConfigTokens()
    api = httpx.AsyncClient(base_url=config.api_url, timeout=10.0, headers=tokens.cabecalho_painel)
    busca = httpx.AsyncClient(
        base_url=config.mcp_conhecimento_url, timeout=10.0, headers=tokens.cabecalho_painel
    )
    app = criar_app(api, busca, config, FonteDeEventosRabbit(ConfigRabbit()))
    # Sem log de acesso: a URL traz a busca por nome de paciente.
    uvicorn.run(app, host="0.0.0.0", port=config.porta, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
