"""Cria o trunk de entrada e a dispatch rule: `scripts/bootstrap_sip.sh`. Pode repetir."""

import asyncio
import ipaddress

from livekit import api
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from clinica_agent.config import ConfigLiveKit

NOME_DO_AGENTE = "recepcionista"
NOME_DO_TRUNK = "gateway-da-clinica"
NOME_DA_REGRA = "ligacoes-para-a-recepcionista"
PREFIXO_DA_SALA = "call-"


class ConfigSip(BaseSettings):
    # env_ignore_empty: o Compose repassa as opcionais vazias.
    model_config = SettingsConfigDict(
        env_prefix="SIP_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    # Só este endereço pode entregar ligações. Um IP ou uma faixa, como 192.168.0.10.
    gateway_ip: str
    # Opcional: o número do chip, se o gateway o entregar no INVITE.
    trunk_numero: str | None = None
    # Opcional: exige digest do gateway, além do IP.
    trunk_usuario: str | None = None
    trunk_senha: SecretStr | None = None


def endereco_valido(valor: str) -> str:
    rede = ipaddress.ip_network(valor.strip(), strict=False)
    # Aberto para o mundo, a trava do trunk não trava nada.
    if rede.prefixlen == 0:
        raise ValueError("SIP_GATEWAY_IP não pode liberar todos os endereços")
    return valor.strip()


def montar_trunk(config: ConfigSip) -> api.SIPInboundTrunkInfo:
    return api.SIPInboundTrunkInfo(
        name=NOME_DO_TRUNK,
        numbers=[config.trunk_numero] if config.trunk_numero else [],
        allowed_addresses=[endereco_valido(config.gateway_ip)],
        auth_username=config.trunk_usuario or "",
        auth_password=config.trunk_senha.get_secret_value() if config.trunk_senha else "",
    )


def montar_regra(trunk_id: str) -> api.SIPDispatchRuleInfo:
    return api.SIPDispatchRuleInfo(
        name=NOME_DA_REGRA,
        trunk_ids=[trunk_id],
        # Nunca a regra individual: ela põe o número de quem liga no nome da sala, e esse nome fica
        # em `chamadas.sala` e nos logs, fora da retenção. A callee usa o número discado, o do chip.
        rule=api.SIPDispatchRule(
            dispatch_rule_callee=api.SIPDispatchRuleCallee(
                room_prefix=PREFIXO_DA_SALA, randomize=True
            )
        ),
        room_config=api.RoomConfiguration(
            agents=[api.RoomAgentDispatch(agent_name=NOME_DO_AGENTE)]
        ),
    )


async def configurar(livekit: api.LiveKitAPI, config: ConfigSip) -> tuple[str, str]:
    """Troca o que já existe: as definições ficam no Redis, e o script é a fonte da verdade."""
    regras = await livekit.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
    for regra in regras.items:
        if regra.name == NOME_DA_REGRA:
            await livekit.sip.delete_dispatch_rule(
                api.DeleteSIPDispatchRuleRequest(sip_dispatch_rule_id=regra.sip_dispatch_rule_id)
            )
    trunks = await livekit.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
    for trunk in trunks.items:
        if trunk.name == NOME_DO_TRUNK:
            await livekit.sip.delete_trunk(
                api.DeleteSIPTrunkRequest(sip_trunk_id=trunk.sip_trunk_id)
            )
    novo = await livekit.sip.create_inbound_trunk(
        api.CreateSIPInboundTrunkRequest(trunk=montar_trunk(config))
    )
    regra = await livekit.sip.create_dispatch_rule(
        api.CreateSIPDispatchRuleRequest(dispatch_rule=montar_regra(novo.sip_trunk_id))
    )
    return novo.sip_trunk_id, regra.sip_dispatch_rule_id


async def _principal() -> None:
    try:
        sip = ConfigSip()  # type: ignore[call-arg]
    except ValidationError as erro:
        raise SystemExit("Defina SIP_GATEWAY_IP no .env, com o IP do gateway.") from erro
    config = ConfigLiveKit()
    async with api.LiveKitAPI(
        url=config.url, api_key=config.api_key, api_secret=config.api_secret.get_secret_value()
    ) as livekit:
        trunk, regra = await configurar(livekit, sip)
    print(f"Trunk {trunk} e regra {regra} criados. Só {sip.gateway_ip} entrega ligações.")


if __name__ == "__main__":
    asyncio.run(_principal())
