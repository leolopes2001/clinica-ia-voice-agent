from dataclasses import dataclass, field
from typing import Any

import pytest
from livekit import api

from clinica_agent.bootstrap_sip import (
    NOME_DA_REGRA,
    NOME_DO_TRUNK,
    ConfigSip,
    configurar,
    endereco_valido,
    montar_regra,
    montar_trunk,
)


def _config(**campos: Any) -> ConfigSip:
    return ConfigSip(**{"_env_file": None, "gateway_ip": "192.168.0.10", **campos})


def test_trunk_so_aceita_o_ip_do_gateway() -> None:
    trunk = montar_trunk(_config())
    assert list(trunk.allowed_addresses) == ["192.168.0.10"]
    assert trunk.name == NOME_DO_TRUNK
    assert trunk.auth_username == ""


def test_trunk_aceita_digest_e_numero_opcionais() -> None:
    trunk = montar_trunk(_config(trunk_usuario="u", trunk_senha="s", trunk_numero="+551130000000"))
    assert (trunk.auth_username, trunk.auth_password) == ("u", "s")
    assert list(trunk.numbers) == ["+551130000000"]


@pytest.mark.parametrize("valor", ["192.168.0.10", "192.168.0.0/24", " 10.0.0.1 "])
def test_enderecos_aceitos(valor: str) -> None:
    assert endereco_valido(valor) == valor.strip()


@pytest.mark.parametrize("valor", ["", "gateway.local", "0.0.0.0/0", "::/0", "999.1.1.1"])
def test_enderecos_recusados(valor: str) -> None:
    with pytest.raises(ValueError):
        endereco_valido(valor)


def test_sala_nunca_traz_o_numero_de_quem_liga() -> None:
    regra = montar_regra("ST_1")
    assert regra.rule.WhichOneof("rule") == "dispatch_rule_callee"
    assert regra.rule.dispatch_rule_callee.room_prefix == "call-"
    # Sem o sufixo, todo mundo cairia na mesma sala.
    assert regra.rule.dispatch_rule_callee.randomize is True


def test_regra_despacha_a_recepcionista_e_vale_so_para_o_trunk() -> None:
    regra = montar_regra("ST_1")
    assert [a.agent_name for a in regra.room_config.agents] == ["recepcionista"]
    assert list(regra.trunk_ids) == ["ST_1"]


@dataclass
class _Sip:
    trunks: list[api.SIPInboundTrunkInfo] = field(default_factory=list)
    regras: list[api.SIPDispatchRuleInfo] = field(default_factory=list)
    criados: int = 0

    async def list_dispatch_rule(self, _: Any) -> Any:
        return api.ListSIPDispatchRuleResponse(items=self.regras)

    async def delete_dispatch_rule(self, pedido: Any) -> None:
        self.regras = [
            r for r in self.regras if r.sip_dispatch_rule_id != pedido.sip_dispatch_rule_id
        ]

    async def list_inbound_trunk(self, _: Any) -> Any:
        return api.ListSIPInboundTrunkResponse(items=self.trunks)

    async def delete_trunk(self, pedido: Any) -> None:
        self.trunks = [t for t in self.trunks if t.sip_trunk_id != pedido.sip_trunk_id]

    async def create_inbound_trunk(self, pedido: Any) -> api.SIPInboundTrunkInfo:
        self.criados += 1
        trunk = api.SIPInboundTrunkInfo()
        trunk.CopyFrom(pedido.trunk)
        trunk.sip_trunk_id = f"ST_{self.criados}"
        self.trunks.append(trunk)
        return trunk

    async def create_dispatch_rule(self, pedido: Any) -> api.SIPDispatchRuleInfo:
        self.criados += 1
        regra = api.SIPDispatchRuleInfo()
        regra.CopyFrom(pedido.dispatch_rule)
        regra.sip_dispatch_rule_id = f"SDR_{self.criados}"
        self.regras.append(regra)
        return regra


@dataclass
class _LiveKit:
    sip: _Sip = field(default_factory=_Sip)


async def test_rodar_duas_vezes_nao_duplica() -> None:
    livekit = _LiveKit()
    await configurar(livekit, _config())  # type: ignore[arg-type]
    await configurar(livekit, _config(gateway_ip="192.168.0.20"))  # type: ignore[arg-type]
    assert [t.name for t in livekit.sip.trunks] == [NOME_DO_TRUNK]
    assert [r.name for r in livekit.sip.regras] == [NOME_DA_REGRA]
    assert list(livekit.sip.trunks[0].allowed_addresses) == ["192.168.0.20"]
    assert list(livekit.sip.regras[0].trunk_ids) == [livekit.sip.trunks[0].sip_trunk_id]


async def test_nao_mexe_em_trunks_de_outra_origem() -> None:
    livekit = _LiveKit()
    livekit.sip.trunks.append(api.SIPInboundTrunkInfo(sip_trunk_id="ST_x", name="outro"))
    await configurar(livekit, _config())  # type: ignore[arg-type]
    assert {t.name for t in livekit.sip.trunks} == {"outro", NOME_DO_TRUNK}
