import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from clinica_core.modelos import Chamada

FILAS_BOAS = [
    {"name": "holds.expirar", "messages": 0, "consumers": 1},
    {"name": "conhecimento.indexar", "messages": 0, "consumers": 1},
    {"name": "chamada.encerrada", "messages": 0, "consumers": 1},
    {"name": "manutencao.diaria", "messages": 0, "consumers": 1},
    {"name": "holds.expirar.dlq", "messages": 0, "consumers": 0},
    {"name": "painel.agendamentos.abc", "messages": 0, "consumers": 1},
]


def _backup(ambiente: Any, **campos: Any) -> None:
    pasta = ambiente.diretorio / "backups"
    pasta.mkdir(exist_ok=True)
    estado = {
        "quando": "2026-11-02T03:30:00-03:00",
        "situacao": "ok",
        "externo": "ok",
        "arquivo": "clinica-x.dump",
        "tamanho": 1,
        **campos,
    }
    (pasta / "ultimo.json").write_text(json.dumps(estado), encoding="utf-8")


@pytest.fixture
def tudo_certo(ambiente: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ambiente.sondas["filas"] = FILAS_BOAS
    _backup(ambiente)
    ambiente.painel.state.config.segredo_sessao = SecretStr("segredo-trocado")
    monkeypatch.setenv("API_TOKEN_PAINEL", "token-trocado")


async def test_recepcao_nao_abre_e_nao_dispara_sondas(recepcao: httpx.AsyncClient) -> None:
    assert (await recepcao.get("/saude-do-sistema")).status_code == 403


@pytest.mark.usefixtures("tudo_certo")
async def test_com_tudo_certo_nao_ha_alertas(admin: httpx.AsyncClient) -> None:
    texto = (await admin.get("/saude-do-sistema")).text
    assert "Tudo em ordem" in texto
    assert 'class="alerta"' not in texto
    assert "holds.expirar" in texto
    assert "painel.agendamentos" not in texto
    assert "Agente de voz" in texto


@pytest.mark.usefixtures("tudo_certo")
@pytest.mark.parametrize(
    ("host", "alerta"),
    [
        ("mcp-clinica", "mcp-clinica não responde"),
        ("mcp-conhecimento", "mcp-conhecimento não responde"),
        ("livekit", "LiveKit não responde"),
        ("agente", "Agente de voz não responde"),
    ],
)
async def test_servico_fora_do_ar_vira_alerta(
    admin: httpx.AsyncClient, ambiente: Any, host: str, alerta: str
) -> None:
    ambiente.sondas["fora"].add(host)
    assert alerta in (await admin.get("/saude-do-sistema")).text


@pytest.mark.usefixtures("tudo_certo")
async def test_rabbit_fora_do_ar_nao_derruba_a_tela(
    admin: httpx.AsyncClient, ambiente: Any
) -> None:
    ambiente.sondas["fora"].add("rabbitmq")
    resposta = await admin.get("/saude-do-sistema")
    assert resposta.status_code == 200
    assert "O RabbitMQ não responde" in resposta.text


@pytest.mark.usefixtures("tudo_certo")
async def test_mensagens_na_dlq_e_worker_parado_viram_alerta(
    admin: httpx.AsyncClient, ambiente: Any
) -> None:
    ambiente.sondas["filas"] = [
        {"name": "holds.expirar", "messages": 0, "consumers": 0},
        {"name": "chamada.encerrada.dlq", "messages": 3, "consumers": 0},
    ]
    texto = (await admin.get("/saude-do-sistema")).text
    assert "O worker não está consumindo holds.expirar" in texto
    assert "chamada.encerrada.dlq tem 3 mensagens com falha" in texto


@pytest.mark.usefixtures("tudo_certo")
@pytest.mark.parametrize(
    ("campos", "alerta"),
    [
        ({"situacao": "falhou"}, "O último backup falhou"),
        ({"externo": "sem_senha"}, "defina BACKUP_SENHA"),
        ({"externo": "falhou"}, "A cópia externa do último backup falhou"),
    ],
)
async def test_problemas_do_backup_viram_alerta(
    admin: httpx.AsyncClient, ambiente: Any, campos: dict[str, str], alerta: str
) -> None:
    _backup(ambiente, **campos)
    assert alerta in (await admin.get("/saude-do-sistema")).text


async def test_sem_backup_e_com_senhas_padrao_avisa(admin: httpx.AsyncClient) -> None:
    texto = (await admin.get("/saude-do-sistema")).text
    assert "Nenhum backup registrado" in texto
    assert "senhas de desenvolvimento em uso" in texto


@pytest.mark.usefixtures("tudo_certo")
async def test_conta_as_chamadas_ativas_e_ignora_as_orfas(
    admin: httpx.AsyncClient, ambiente: Any
) -> None:
    from datetime import timedelta
    from uuid import uuid4

    from sqlalchemy import func, select

    async with ambiente.api.state.fabrica_sessoes() as sessao:
        agora = (await sessao.execute(select(func.now()))).scalar_one()
        for inicio, fim in (
            (agora, None),
            (agora, agora),
            (agora - timedelta(hours=5), None),
        ):
            sessao.add(
                Chamada(
                    clinica_id=ambiente.clinica_id,
                    telefone="+5511900000001",
                    sala=f"s-{uuid4()}",
                    inicio=inicio,
                    fim=fim,
                )
            )
        await sessao.commit()
    assert "<strong>1</strong>" in (await admin.get("/saude-do-sistema")).text
