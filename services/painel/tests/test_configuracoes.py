from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from clinica_api.agenda import ErroAgenda
from clinica_api.configuracao_agente import dentro_do_horario, destino_de_transferencia
from clinica_core.config import ConfigTokens
from clinica_core.modelos import (
    Auditoria,
    Chamada,
    ConfiguracaoAgente,
    Consultorio,
    Dentista,
    GradeTrabalho,
    Procedimento,
)

FUSO = ZoneInfo("America/Sao_Paulo")
# Uma segunda-feira às 10h no horário de Brasília.
SEGUNDA_10H = datetime(2026, 11, 2, 10, tzinfo=FUSO)


async def _linha(ambiente: Any) -> ConfiguracaoAgente | None:
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        linha: ConfiguracaoAgente | None = await sessao.get(ConfiguracaoAgente, ambiente.clinica_id)
        return linha


async def test_so_administrador_abre_as_configuracoes(recepcao: httpx.AsyncClient) -> None:
    assert (await recepcao.get("/configuracoes")).status_code == 403
    assert (await recepcao.post("/configuracoes", data={"voz": "Kore"})).status_code == 403


async def test_salva_tudo_e_audita(admin: httpx.AsyncClient, ambiente: Any) -> None:
    resposta = await admin.post(
        "/configuracoes",
        data={
            "saudacao": "Bom dia, aqui é a {clinica}.",
            "voz": "Kore",
            "numero_transferencia": "(11) 98888-7777",
            "antecedencia_minima_min": "60",
            "inicio_0": "08:00",
            "fim_0": "18:00",
        },
    )
    assert resposta.status_code == 303, resposta.text
    linha = await _linha(ambiente)
    assert linha is not None
    assert linha.saudacao == "Bom dia, aqui é a {clinica}."
    assert linha.voz == "Kore"
    assert linha.numero_transferencia == "+5511988887777"
    assert linha.antecedencia_minima_min == 60
    assert linha.horario_comercial == {"0": {"inicio": "08:00:00", "fim": "18:00:00"}}
    tela = (await admin.get("/configuracoes?salvo=1")).text
    assert "Salvo." in tela
    assert 'value="08:00"' in tela
    assert 'value="+5511988887777"' in tela
    assert '<option value="Kore" selected' in tela
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        registro = await sessao.scalar(
            select(Auditoria).where(Auditoria.entidade == "configuracao_agente")
        )
    assert registro is not None
    assert registro.usuario_id == ambiente.admin_id
    assert registro.depois is not None
    assert registro.depois["voz"] == "Kore"


async def test_salvar_em_branco_volta_aos_padroes(admin: httpx.AsyncClient, ambiente: Any) -> None:
    await admin.post("/configuracoes", data={"voz": "Kore", "antecedencia_minima_min": "60"})
    await admin.post("/configuracoes", data={})
    linha = await _linha(ambiente)
    assert linha is not None
    assert (linha.voz, linha.antecedencia_minima_min, linha.horario_comercial) == (None, None, None)


@pytest.mark.parametrize(
    "dados",
    [
        {"voz": "VozInventada"},
        {"numero_transferencia": "123"},
        {"antecedencia_minima_min": "-1"},
        {"antecedencia_minima_min": "abc"},
        {"inicio_0": "18:00", "fim_0": "08:00"},
        {"inicio_0": "08:00"},
    ],
)
async def test_valores_invalidos_sao_recusados(
    admin: httpx.AsyncClient, ambiente: Any, dados: dict[str, str]
) -> None:
    assert (await admin.post("/configuracoes", data=dados)).status_code == 422
    assert await _linha(ambiente) is None


def test_destino_aceita_telefone_e_uri_sip() -> None:
    assert destino_de_transferencia("11 98888-7777") == "+5511988887777"
    assert destino_de_transferencia("sip:100@192.168.0.20") == "sip:100@192.168.0.20"
    with pytest.raises(ErroAgenda):
        destino_de_transferencia("recepção")


@pytest.mark.parametrize(
    ("agora", "esperado"),
    [
        (SEGUNDA_10H, True),
        (SEGUNDA_10H.replace(hour=8), True),
        (SEGUNDA_10H.replace(hour=18), False),
        (SEGUNDA_10H.replace(hour=7, minute=59), False),
        (SEGUNDA_10H + timedelta(days=1), False),
        # 13h em UTC é 10h em Brasília.
        (datetime(2026, 11, 2, 13, tzinfo=UTC), True),
    ],
)
def test_dentro_do_horario(agora: datetime, esperado: bool) -> None:
    horario = {"0": {"inicio": "08:00:00", "fim": "18:00:00"}}
    assert dentro_do_horario(horario, agora, FUSO) is esperado


def test_sem_horario_cadastrado_nunca_esta_fora() -> None:
    assert dentro_do_horario(None, SEGUNDA_10H, FUSO) is True
    assert dentro_do_horario({}, SEGUNDA_10H, FUSO) is True


async def _api(ambiente: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=ambiente.api), base_url="http://api")


async def test_ligacao_recebe_a_configuracao(admin: httpx.AsyncClient, ambiente: Any) -> None:
    await admin.post(
        "/configuracoes",
        data={"saudacao": "Oi", "voz": "Puck", "numero_transferencia": "sip:100@10.0.0.1"},
    )
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        chamada = Chamada(
            clinica_id=ambiente.clinica_id, telefone="+5511900000001", sala=f"s-{uuid4()}"
        )
        sessao.add(chamada)
        await sessao.commit()
        chamada_id = chamada.id
    async with await _api(ambiente) as api:
        resposta = await api.get(
            "/chamadas/configuracao",
            headers={**ConfigTokens().cabecalho_servicos, "X-Chamada-Id": str(chamada_id)},
        )
    assert resposta.json() == {
        "saudacao": "Oi",
        "voz": "Puck",
        "dentro_do_horario": True,
        "numero_transferencia": "sip:100@10.0.0.1",
        "retencao_dias": 90,
    }


async def test_configuracao_da_ligacao_exige_uma_chamada(ambiente: Any) -> None:
    async with await _api(ambiente) as api:
        resposta = await api.get(
            "/chamadas/configuracao",
            headers={**ConfigTokens().cabecalho_painel, "X-Clinica-Id": str(ambiente.clinica_id)},
        )
    assert resposta.json()["motivo"] == "exige_chamada"


async def test_antecedencia_configurada_muda_os_horarios(
    admin: httpx.AsyncClient, ambiente: Any
) -> None:
    # O teste sobe a API com antecedência 0. Três dias de antecedência tiram os horários de amanhã.
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        cid = ambiente.clinica_id
        dentista = Dentista(clinica_id=cid, nome="Dra. Ana")
        consultorio = Consultorio(clinica_id=cid, nome="Sala")
        sessao.add_all([dentista, consultorio])
        await sessao.flush()
        procedimento = Procedimento(
            clinica_id=cid, nome="Limpeza", duracao_min=30, dentistas_habilitados=[dentista.id]
        )
        sessao.add(procedimento)
        amanha = date.today() + timedelta(days=1)
        sessao.add(
            GradeTrabalho(
                clinica_id=cid,
                dentista_id=dentista.id,
                consultorio_id=consultorio.id,
                dia_semana=amanha.weekday(),
                inicio=time(0),
                fim=time(23),
            )
        )
        await sessao.commit()
        parametros = {
            "procedimento_id": str(procedimento.id),
            "dentista_id": str(dentista.id),
            "data": amanha.isoformat(),
        }
    antes = (await admin.get("/agenda/horarios", params=parametros)).text
    assert "00:00" in antes
    await admin.post("/configuracoes", data={"antecedencia_minima_min": str(60 * 24 * 3)})
    depois = (await admin.get("/agenda/horarios", params=parametros)).text
    assert "Nenhum horário livre" in depois
