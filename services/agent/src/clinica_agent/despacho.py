import json
from uuid import uuid4

from livekit import api

from clinica_agent.config import ConfigLiveKit
from clinica_agent.principal import NOME_AGENTE


async def despachar_chamada_de_teste(config: ConfigLiveKit, telefone: str) -> tuple[str, str]:
    """Abre uma sala com o agente e devolve o nome dela e o token do participante de teste."""
    sala = f"teste-{uuid4().hex[:12]}"
    segredo = config.api_secret.get_secret_value()
    async with api.LiveKitAPI(
        url=config.url, api_key=config.api_key, api_secret=segredo
    ) as livekit:
        await livekit.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=NOME_AGENTE,
                room=sala,
                metadata=json.dumps({"telefone_de_teste": telefone}),
            )
        )
    token = (
        api.AccessToken(config.api_key, segredo)
        .with_identity(f"paciente-{uuid4().hex[:8]}")
        .with_grants(api.VideoGrants(room_join=True, room=sala))
        .to_jwt()
    )
    return sala, token
