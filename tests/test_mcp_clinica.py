import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import httpx2
import pytest
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clinica_api.app import criar_app as criar_api
from clinica_core.config import ConfigAgenda, ConfigPostgres
from clinica_core.eventos import EventoAgendamento
from clinica_core.modelos import (
    Agendamento,
    Chamada,
    Clinica,
    Consultorio,
    Dentista,
    GradeTrabalho,
    Paciente,
    PedidoRetorno,
    Procedimento,
)
from clinica_mcp_clinica.servidor import criar_app, criar_servidor

FUSO = ZoneInfo("America/Sao_Paulo")
TELEFONE = "+5511900000001"
FERRAMENTAS = {
    "registrar_consentimento",
    "identificar_chamador",
    "selecionar_paciente",
    "cadastrar_paciente",
    "verificar_identidade",
    "listar_procedimentos",
    "listar_horarios_livres",
    "segurar_horario",
    "confirmar_agendamento",
    "listar_meus_agendamentos",
    "remarcar_agendamento",
    "cancelar_agendamento",
    "registrar_pedido_retorno",
}
Fabrica = async_sessionmaker[AsyncSession]


class PublicadorFalso:
    async def abrir(self) -> None:
        pass

    async def fechar(self) -> None:
        pass

    async def hold_criado(self, hold_id: UUID, duracao: timedelta) -> None:
        pass

    async def eventos(self, eventos: Sequence[EventoAgendamento]) -> None:
        pass


@dataclass
class Cenario:
    clinica_id: UUID
    # Segunda-feira futura, com a Dra. Ana das 8h às 19h.
    dia: date
    maria: UUID
    joao: UUID

    def h(self, hora: int, minuto: int = 0) -> datetime:
        return datetime.combine(self.dia, time(hora, minuto), tzinfo=FUSO)


@pytest.fixture
async def api(banco: str) -> AsyncIterator[FastAPI]:
    app = criar_api(
        ConfigPostgres(db=banco), ConfigAgenda(antecedencia_minima_min=0), PublicadorFalso()
    )
    yield app
    await app.state.engine.dispose()


@pytest.fixture
def fabrica(api: FastAPI) -> Fabrica:
    fabrica: Fabrica = api.state.fabrica_sessoes
    return fabrica


@pytest.fixture
async def http_api(api: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transporte = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transporte, base_url="http://api") as cliente:
        yield cliente


@pytest.fixture
def app_mcp(http_api: httpx.AsyncClient) -> httpx.AsyncClient:
    return http_api


@pytest.fixture
async def c(fabrica: Fabrica) -> Cenario:
    # Telefone próprio por teste: o banco é compartilhado no módulo.
    async with fabrica() as sessao:
        clinica = Clinica(
            nome="Clínica", telefone="+5511", endereco="R", politica_cancelamento_horas=24
        )
        sessao.add(clinica)
        await sessao.flush()
        ana = Dentista(clinica_id=clinica.id, nome="Dra. Ana Souza")
        consultorio = Consultorio(clinica_id=clinica.id, nome="1")
        maria = Paciente(
            clinica_id=clinica.id,
            nome="Maria Lima",
            telefone_e164=TELEFONE,
            data_nascimento=date(1990, 1, 1),
        )
        joao = Paciente(
            clinica_id=clinica.id,
            nome="João Lima",
            telefone_e164=TELEFONE,
            data_nascimento=date(2015, 5, 5),
        )
        sessao.add_all([ana, consultorio, maria, joao])
        await sessao.flush()
        sessao.add(
            Procedimento(
                clinica_id=clinica.id,
                nome="Limpeza",
                sinonimos=["Profilaxia"],
                duracao_min=30,
                dentistas_habilitados=[ana.id],
            )
        )
        hoje = (await sessao.execute(select(func.now()))).scalar_one().astimezone(FUSO).date()
        dia = hoje + timedelta(days=14 - hoje.weekday())
        sessao.add(
            GradeTrabalho(
                clinica_id=clinica.id,
                dentista_id=ana.id,
                consultorio_id=consultorio.id,
                dia_semana=dia.weekday(),
                inicio=time(8),
                fim=time(19),
            )
        )
        await sessao.commit()
        return Cenario(clinica.id, dia, maria.id, joao.id)


async def _nova_chamada(
    http_api: httpx.AsyncClient,
    c: Cenario,
    telefone: str = TELEFONE,
    sala: str | None = None,
    consentiu: bool = True,
) -> str:
    resposta = await http_api.post(
        "/chamadas",
        headers={"X-Clinica-Id": str(c.clinica_id), "Idempotency-Key": uuid4().hex},
        json={"telefone": telefone, "sala": sala or f"call-{uuid4()}"},
    )
    assert resposta.status_code == 201, resposta.text
    chamada_id: str = resposta.json()["chamada_id"]
    if consentiu:
        registrado = await http_api.post(
            "/chamadas/consentimento",
            headers={"X-Chamada-Id": chamada_id},
            json={"aceitou": True},
        )
        assert registrado.json() == {"consentimento": True}
    return chamada_id


@asynccontextmanager
async def _cliente_mcp(api: httpx.AsyncClient, cabecalhos: dict[str, str]) -> AsyncIterator[Client]:
    # Sem fixture: o lifespan e o Client abrem cancel scopes do anyio, presos à mesma tarefa.
    # Um app por conexão: o gerenciador de sessões do MCP só pode rodar uma vez.
    app = criar_app(api)
    async with app.router.lifespan_context(app):
        transporte = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transporte, base_url="http://mcp", headers=cabecalhos
        ) as http:
            async with Client(
                streamable_http_client("http://mcp/mcp", http_client=http)
            ) as cliente:
                yield cliente


@asynccontextmanager
async def _na_chamada(api: httpx.AsyncClient, chamada_id: str) -> AsyncIterator[Client]:
    async with _cliente_mcp(api, {"X-Chamada-Id": chamada_id}) as cliente:
        yield cliente


async def _ferramenta(cliente: Client, ferramenta: str, /, **argumentos: Any) -> dict[str, Any]:
    resultado = await cliente.call_tool(ferramenta, argumentos)
    assert not resultado.is_error, resultado.content
    corpo: dict[str, Any] = resultado.structured_content or {}
    return corpo


async def _quantos_confirmados(fabrica: Fabrica, chamada_id: str) -> int:
    async with fabrica() as sessao:
        return (
            await sessao.execute(
                select(func.count())
                .select_from(Agendamento)
                .where(
                    Agendamento.chamada_id == UUID(chamada_id),
                    Agendamento.status == "confirmado",
                )
            )
        ).scalar_one()


# ---- Contrato


async def test_expoe_as_13_ferramentas_sem_telefone_nem_clinica() -> None:
    servidor = criar_servidor(httpx.AsyncClient())
    ferramentas = await servidor.list_tools()
    assert {f.name for f in ferramentas} == FERRAMENTAS
    for ferramenta in ferramentas:
        parametros = set(ferramenta.input_schema.get("properties", {}))
        assert not parametros & {"telefone", "clinica_id", "chamada_id", "ctx"}, ferramenta.name
        # paciente_id só entra em selecionar_paciente, com a lista do próprio telefone.
        if ferramenta.name != "selecionar_paciente":
            assert "paciente_id" not in parametros, ferramenta.name


async def test_sem_header_de_chamada_recusa_a_ferramenta(app_mcp: httpx.AsyncClient) -> None:
    async with _cliente_mcp(app_mcp, {}) as cliente:
        resultado = await cliente.call_tool("identificar_chamador", {})
    assert resultado.is_error


async def test_chamada_inexistente_vira_falha(app_mcp: httpx.AsyncClient) -> None:
    async with _na_chamada(app_mcp, str(uuid4())) as cliente:
        corpo = await _ferramenta(cliente, "identificar_chamador")
    assert corpo == {"ok": False, "motivo": "chamada_nao_encontrada"}


async def test_saude_do_servidor(app_mcp: httpx.AsyncClient) -> None:
    transporte = httpx.ASGITransport(app=criar_app(app_mcp))
    async with httpx.AsyncClient(transport=transporte, base_url="http://mcp") as http:
        assert (await http.get("/saude")).json() == {"status": "ok"}


async def test_api_fora_do_ar_vira_falha() -> None:
    def recusar(pedido: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("fora do ar", request=pedido)

    api = httpx.AsyncClient(transport=httpx.MockTransport(recusar), base_url="http://api")
    async with _na_chamada(api, str(uuid4())) as cliente:
        corpo = await _ferramenta(cliente, "identificar_chamador")
    assert corpo == {"ok": False, "motivo": "servico_indisponivel"}


# ---- POST /chamadas


async def test_criar_chamada_idempotente_pela_chave(
    http_api: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    cabecalhos = {"X-Clinica-Id": str(c.clinica_id), "Idempotency-Key": uuid4().hex}
    corpo = {"telefone": TELEFONE, "sala": "call-idempotente"}
    primeira = await http_api.post("/chamadas", headers=cabecalhos, json=corpo)
    segunda = await http_api.post("/chamadas", headers=cabecalhos, json=corpo)
    assert primeira.status_code == segunda.status_code == 201
    assert primeira.json() == segunda.json()
    async with fabrica() as sessao:
        chamada = await sessao.get_one(Chamada, UUID(primeira.json()["chamada_id"]))
    assert (chamada.telefone, chamada.clinica_id) == (TELEFONE, c.clinica_id)


@pytest.mark.parametrize("telefone", ["11999990000", "+0551199999", "+55 11 99999", ""])
async def test_criar_chamada_exige_e164(
    http_api: httpx.AsyncClient, c: Cenario, telefone: str
) -> None:
    resposta = await http_api.post(
        "/chamadas",
        headers={"X-Clinica-Id": str(c.clinica_id), "Idempotency-Key": uuid4().hex},
        json={"telefone": telefone, "sala": "call"},
    )
    assert resposta.status_code == 422


async def test_criar_chamada_recusa_clinica_no_corpo(
    http_api: httpx.AsyncClient, c: Cenario
) -> None:
    resposta = await http_api.post(
        "/chamadas",
        headers={"X-Clinica-Id": str(c.clinica_id), "Idempotency-Key": uuid4().hex},
        json={"telefone": TELEFONE, "sala": "call", "clinica_id": str(uuid4())},
    )
    assert resposta.status_code == 422


async def test_criar_chamada_recusa_vir_de_outra_chamada(
    http_api: httpx.AsyncClient, c: Cenario
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    resposta = await http_api.post(
        "/chamadas",
        headers={"X-Chamada-Id": chamada_id, "Idempotency-Key": uuid4().hex},
        json={"telefone": TELEFONE, "sala": "call"},
    )
    assert resposta.status_code == 400
    assert resposta.json()["motivo"] == "exige_painel"


@pytest.mark.parametrize(
    "caminho",
    ["/identidade/identificar", "/agendamentos/meus"],
)
async def test_rotas_de_identidade_exigem_chamada(
    http_api: httpx.AsyncClient, c: Cenario, caminho: str
) -> None:
    cabecalhos = {"X-Clinica-Id": str(c.clinica_id)}
    if caminho.startswith("/identidade"):
        resposta = await http_api.post(caminho, headers=cabecalhos)
    else:
        resposta = await http_api.get(caminho, headers=cabecalhos)
    assert resposta.status_code == 400
    assert resposta.json()["motivo"] == "exige_chamada"


# ---- Identidade (H1, H3, H5)


async def test_varios_pacientes_no_telefone_nao_seleciona_nenhum(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        corpo = await _ferramenta(cliente, "identificar_chamador")
    assert corpo["pacientes"] == [
        {"paciente_id": str(c.joao), "primeiro_nome": "João"},
        {"paciente_id": str(c.maria), "primeiro_nome": "Maria"},
    ]
    async with fabrica() as sessao:
        assert (await sessao.get_one(Chamada, UUID(chamada_id))).paciente_id is None


async def test_um_unico_paciente_fica_selecionado(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    telefone = "+5511900000002"
    async with fabrica() as sessao:
        unico = Paciente(
            clinica_id=c.clinica_id,
            nome="Ana Paula",
            telefone_e164=telefone,
            data_nascimento=date(1980, 2, 2),
        )
        sessao.add(unico)
        await sessao.commit()
    chamada_id = await _nova_chamada(http_api, c, telefone)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        corpo = await _ferramenta(cliente, "identificar_chamador")
        assert [p["primeiro_nome"] for p in corpo["pacientes"]] == ["Ana"]
        assert (await _ferramenta(cliente, "verificar_identidade", data_nascimento="1980-02-02"))[
            "ok"
        ]
        # Identificar de novo não desfaz a verificação do mesmo paciente.
        await _ferramenta(cliente, "identificar_chamador")
        assert (await _ferramenta(cliente, "listar_meus_agendamentos"))["consultas"] == []


async def test_telefone_sem_cadastro_devolve_lista_vazia(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario
) -> None:
    chamada_id = await _nova_chamada(http_api, c, "+5511900000003")
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        assert (await _ferramenta(cliente, "identificar_chamador"))["pacientes"] == []


async def test_selecionar_recusa_paciente_de_outro_telefone(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario
) -> None:
    chamada_id = await _nova_chamada(http_api, c, "+5511900000004")
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        corpo = await _ferramenta(cliente, "selecionar_paciente", paciente_id=str(c.maria))
    assert corpo == {"ok": False, "motivo": "paciente_nao_permitido"}


async def test_trocar_de_paciente_desfaz_verificacao_e_nao_zera_tentativas(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        await _ferramenta(cliente, "selecionar_paciente", paciente_id=str(c.maria))
        errada = await _ferramenta(cliente, "verificar_identidade", data_nascimento="2000-01-01")
        assert (errada["ok"], errada["tentativas_restantes"]) == (False, 2)
        certa = await _ferramenta(cliente, "verificar_identidade", data_nascimento="1990-01-01")
        assert (certa["ok"], certa["tentativas_restantes"]) == (True, 2)
        assert (await _ferramenta(cliente, "listar_meus_agendamentos"))["ok"]

        await _ferramenta(cliente, "selecionar_paciente", paciente_id=str(c.joao))
        negado = await _ferramenta(cliente, "listar_meus_agendamentos")
        assert negado == {"ok": False, "motivo": "identidade_nao_verificada"}
    async with fabrica() as sessao:
        chamada = await sessao.get_one(Chamada, UUID(chamada_id))
    assert chamada.paciente_id == c.joao
    assert chamada.identidade_verificada_em is None
    assert chamada.tentativas_identidade == 1


async def test_tres_erros_bloqueiam_ate_a_data_certa(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        await _ferramenta(cliente, "selecionar_paciente", paciente_id=str(c.maria))
        for restantes in (2, 1, 0):
            corpo = await _ferramenta(cliente, "verificar_identidade", data_nascimento="2000-01-01")
            assert (corpo["ok"], corpo["tentativas_restantes"]) == (False, restantes)
        corpo = await _ferramenta(cliente, "verificar_identidade", data_nascimento="1990-01-01")
        assert (corpo["ok"], corpo["motivo"]) == (False, "tentativas_esgotadas")
        assert (await _ferramenta(cliente, "listar_meus_agendamentos"))["ok"] is False


async def test_erro_depois_de_verificar_desfaz_a_verificacao(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        await _ferramenta(cliente, "selecionar_paciente", paciente_id=str(c.maria))
        await _ferramenta(cliente, "verificar_identidade", data_nascimento="1990-01-01")
        await _ferramenta(cliente, "verificar_identidade", data_nascimento="2000-01-01")
        negado = await _ferramenta(cliente, "listar_meus_agendamentos")
    assert negado["motivo"] == "identidade_nao_verificada"


async def test_tentativas_simultaneas_nao_passam_do_limite(
    http_api: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    cabecalhos = {"X-Chamada-Id": chamada_id}
    await http_api.post(
        "/identidade/selecionar", headers=cabecalhos, json={"paciente_id": str(c.maria)}
    )
    await asyncio.gather(
        *(
            http_api.post(
                "/identidade/verificar",
                headers=cabecalhos,
                json={"data_nascimento": "2000-01-01"},
            )
            for _ in range(10)
        )
    )
    async with fabrica() as sessao:
        assert (await sessao.get_one(Chamada, UUID(chamada_id))).tentativas_identidade == 3


async def test_verificar_sem_paciente_selecionado(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        corpo = await _ferramenta(cliente, "verificar_identidade", data_nascimento="1990-01-01")
    assert corpo == {"ok": False, "motivo": "paciente_nao_selecionado"}


async def test_cadastrar_novo_fica_selecionado_com_telefone_da_ligacao(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    telefone = "+5511900000005"
    chamada_id = await _nova_chamada(http_api, c, telefone)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        corpo = await _ferramenta(
            cliente,
            "cadastrar_paciente",
            nome="Carla Dias",
            data_nascimento="1985-03-03",
        )
    assert corpo["primeiro_nome"] == "Carla"
    async with fabrica() as sessao:
        chamada = await sessao.get_one(Chamada, UUID(chamada_id))
        paciente = await sessao.get_one(Paciente, UUID(corpo["paciente_id"]))
    assert chamada.paciente_id == paciente.id
    assert chamada.identidade_verificada_em is None
    assert paciente.telefone_e164 == telefone


async def test_mesma_pessoa_nao_e_cadastrada_duas_vezes_no_telefone(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        corpo = await _ferramenta(
            cliente,
            "cadastrar_paciente",
            nome="  maria   LIMA ",
            data_nascimento="1990-01-01",
        )
    assert corpo["paciente_id"] == str(c.maria)
    async with fabrica() as sessao:
        total = (
            await sessao.execute(
                select(func.count())
                .select_from(Paciente)
                .where(Paciente.clinica_id == c.clinica_id, Paciente.telefone_e164 == TELEFONE)
            )
        ).scalar_one()
    assert total == 2


async def test_outra_pessoa_no_mesmo_telefone_e_cadastrada(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        corpo = await _ferramenta(
            cliente, "cadastrar_paciente", nome="Pedro Lima", data_nascimento="2018-08-08"
        )
    assert corpo["paciente_id"] not in {str(c.maria), str(c.joao)}


async def test_cadastrar_recusa_nascimento_no_futuro(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        corpo = await _ferramenta(
            cliente, "cadastrar_paciente", nome="Pedro Lima", data_nascimento="2999-01-01"
        )
    assert corpo["motivo"] == "data_nascimento_invalida"


# ---- Procedimentos e horários (H3)


async def test_listar_procedimentos(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        corpo = await _ferramenta(cliente, "listar_procedimentos")
    assert corpo["procedimentos"] == [
        {"nome": "Limpeza", "duracao_min": 30, "sinonimos": ["Profilaxia"]}
    ]


async def test_listar_horarios_ate_tres_com_fala(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        corpo = await _ferramenta(
            cliente,
            "listar_horarios_livres",
            procedimento="profilaxia",
            a_partir_de=c.dia.isoformat(),
            ate=c.dia.isoformat(),
        )
    opcoes = corpo["opcoes"]
    assert len(opcoes) == 3
    assert opcoes[0]["fala"] == f"segunda, dia {c.dia.day}, às 8h, com Dra. Ana Souza"
    locais = [datetime.fromisoformat(o["inicio"]).astimezone(FUSO) for o in opcoes]
    assert [local.hour >= 18 for local in locais] == [False, False, True]


@pytest.mark.parametrize(
    ("turno", "horas"),
    [("manha", range(8, 12)), ("tarde", range(12, 18)), ("noite", range(18, 19))],
)
async def test_listar_horarios_filtra_turno_antes_de_escolher(
    app_mcp: httpx.AsyncClient,
    http_api: httpx.AsyncClient,
    c: Cenario,
    turno: str,
    horas: range,
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        corpo = await _ferramenta(
            cliente,
            "listar_horarios_livres",
            procedimento="Limpeza",
            a_partir_de=c.dia.isoformat(),
            ate=c.dia.isoformat(),
            turno=turno,
        )
    assert len(corpo["opcoes"]) == 3
    for opcao in corpo["opcoes"]:
        assert datetime.fromisoformat(opcao["inicio"]).astimezone(FUSO).hour in horas


async def test_listar_horarios_com_nome_parcial_do_dentista(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        corpo = await _ferramenta(
            cliente,
            "listar_horarios_livres",
            procedimento="Limpeza",
            a_partir_de=c.dia.isoformat(),
            ate=c.dia.isoformat(),
            dentista="doutora ana",
        )
    assert {o["dentista"] for o in corpo["opcoes"]} == {"Dra. Ana Souza"}


@pytest.mark.parametrize(
    ("argumentos", "motivo"),
    [
        ({"procedimento": "Clareamento"}, "procedimento_nao_encontrado"),
        ({"procedimento": "Limpeza", "dentista": "Carlos"}, "dentista_nao_encontrado"),
        ({"procedimento": "Limpeza", "a_partir_de": "amanhã"}, "entrada_invalida"),
    ],
)
async def test_listar_horarios_entrada_invalida(
    app_mcp: httpx.AsyncClient,
    http_api: httpx.AsyncClient,
    c: Cenario,
    argumentos: dict[str, str],
    motivo: str,
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    padrao = {"a_partir_de": c.dia.isoformat(), "ate": c.dia.isoformat()}
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        corpo = await _ferramenta(cliente, "listar_horarios_livres", **{**padrao, **argumentos})
    assert corpo == {"ok": False, "motivo": motivo}


async def test_dentista_ambiguo_e_recusado(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    async with fabrica() as sessao:
        sessao.add(Dentista(clinica_id=c.clinica_id, nome="Dra. Ana Beatriz"))
        await sessao.commit()
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        corpo = await _ferramenta(
            cliente,
            "segurar_horario",
            procedimento="Limpeza",
            dentista="Ana",
            inicio=c.h(9).isoformat(),
        )
    assert corpo == {"ok": False, "motivo": "dentista_ambiguo"}


# ---- Agendar, remarcar e cancelar (H3 a H6)


async def _segurar(cliente: Client, c: Cenario, inicio: datetime) -> dict[str, Any]:
    return await _ferramenta(
        cliente,
        "segurar_horario",
        procedimento="Limpeza",
        dentista="Dra. Ana Souza",
        inicio=inicio.isoformat(),
    )


async def test_agendar_do_inicio_ao_fim_com_retries(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        await _ferramenta(cliente, "selecionar_paciente", paciente_id=str(c.maria))
        hold = await _segurar(cliente, c, c.h(9))
        assert hold["ok"]
        assert hold["fala"] == f"segunda, dia {c.dia.day}, às 9h, com Dra. Ana Souza"
        assert hold["procedimento"] == "Limpeza"
        # Um retry devolve o mesmo hold, em vez de "ocupado" pelo próprio hold.
        assert (await _segurar(cliente, c, c.h(9)))["hold_id"] == hold["hold_id"]

        primeira = await _ferramenta(cliente, "confirmar_agendamento", hold_id=hold["hold_id"])
        segunda = await _ferramenta(cliente, "confirmar_agendamento", hold_id=hold["hold_id"])
    assert primeira["ok"] and primeira == segunda
    assert primeira["paciente_id"] == str(c.maria)
    assert await _quantos_confirmados(fabrica, chamada_id) == 1


async def test_confirmar_sem_paciente_selecionado_falha(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        hold = await _segurar(cliente, c, c.h(10))
        corpo = await _ferramenta(cliente, "confirmar_agendamento", hold_id=hold["hold_id"])
    assert corpo == {"ok": False, "motivo": "paciente_nao_selecionado"}


async def test_horario_ocupado_traz_ate_tres_alternativas_faladas(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario
) -> None:
    primeira = await _nova_chamada(http_api, c)
    segunda = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, primeira) as cliente:
        assert (await _segurar(cliente, c, c.h(11)))["ok"]
    async with _na_chamada(app_mcp, segunda) as cliente:
        corpo = await _segurar(cliente, c, c.h(11))
    assert (corpo["ok"], corpo["motivo"]) == (False, "ocupado")
    assert 1 <= len(corpo["alternativas"]) <= 3
    assert all(a["fala"].endswith("com Dra. Ana Souza") for a in corpo["alternativas"])


async def test_hold_de_outra_chamada_nao_confirma(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario
) -> None:
    primeira = await _nova_chamada(http_api, c)
    segunda = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, primeira) as cliente:
        hold = await _segurar(cliente, c, c.h(12))
    async with _na_chamada(app_mcp, segunda) as cliente:
        await _ferramenta(cliente, "selecionar_paciente", paciente_id=str(c.maria))
        corpo = await _ferramenta(cliente, "confirmar_agendamento", hold_id=hold["hold_id"])
    assert corpo == {"ok": False, "motivo": "hold_perdido"}


async def _consulta_de_maria(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario, inicio: datetime
) -> str:
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        await _ferramenta(cliente, "selecionar_paciente", paciente_id=str(c.maria))
        hold = await _segurar(cliente, c, inicio)
        corpo = await _ferramenta(cliente, "confirmar_agendamento", hold_id=hold["hold_id"])
    agendamento_id: str = corpo["agendamento_id"]
    return agendamento_id


async def test_listar_meus_so_do_paciente_verificado_e_remarcar(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    antigo = await _consulta_de_maria(app_mcp, http_api, c, c.h(13))
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        await _ferramenta(cliente, "selecionar_paciente", paciente_id=str(c.joao))
        await _ferramenta(cliente, "verificar_identidade", data_nascimento="2015-05-05")
        assert (await _ferramenta(cliente, "listar_meus_agendamentos"))["consultas"] == []

        await _ferramenta(cliente, "selecionar_paciente", paciente_id=str(c.maria))
        await _ferramenta(cliente, "verificar_identidade", data_nascimento="1990-01-01")
        consultas = (await _ferramenta(cliente, "listar_meus_agendamentos"))["consultas"]
        assert antigo in {consulta["agendamento_id"] for consulta in consultas}
        fala = next(cons["fala"] for cons in consultas if cons["agendamento_id"] == antigo)
        assert fala == f"Limpeza, segunda, dia {c.dia.day}, às 13h, com Dra. Ana Souza"

        hold = await _segurar(cliente, c, c.h(14))
        novo = await _ferramenta(
            cliente, "remarcar_agendamento", agendamento_id=antigo, hold_id=hold["hold_id"]
        )
        repetido = await _ferramenta(
            cliente, "remarcar_agendamento", agendamento_id=antigo, hold_id=hold["hold_id"]
        )
    assert novo["ok"] and novo == repetido
    async with fabrica() as sessao:
        assert (await sessao.get_one(Agendamento, UUID(antigo))).status == "cancelado"
        assert (
            await sessao.get_one(Agendamento, UUID(novo["agendamento_id"]))
        ).status == "confirmado"


async def test_cancelar_exige_identidade_e_traz_o_aviso(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    agendamento = await _consulta_de_maria(app_mcp, http_api, c, c.h(15))
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        await _ferramenta(cliente, "selecionar_paciente", paciente_id=str(c.maria))
        negado = await _ferramenta(cliente, "cancelar_agendamento", agendamento_id=agendamento)
        assert negado == {"ok": False, "motivo": "identidade_nao_verificada"}

        await _ferramenta(cliente, "verificar_identidade", data_nascimento="1990-01-01")
        corpo = await _ferramenta(
            cliente, "cancelar_agendamento", agendamento_id=agendamento, motivo="viagem"
        )
    assert corpo["ok"]
    assert corpo["dentro_do_prazo"] is True
    assert corpo["aviso"] == "Cancelado dentro do prazo de 24 horas da política da clínica."
    async with fabrica() as sessao:
        cancelada = await sessao.get_one(Agendamento, UUID(agendamento))
    assert (cancelada.status, cancelada.motivo_cancelamento) == ("cancelado", "viagem")


async def test_cancelar_consulta_de_outro_paciente_responde_inexistente(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario
) -> None:
    agendamento = await _consulta_de_maria(app_mcp, http_api, c, c.h(16))
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        await _ferramenta(cliente, "selecionar_paciente", paciente_id=str(c.joao))
        await _ferramenta(cliente, "verificar_identidade", data_nascimento="2015-05-05")
        corpo = await _ferramenta(cliente, "cancelar_agendamento", agendamento_id=agendamento)
    assert corpo == {"ok": False, "motivo": "agendamento_nao_encontrado"}


# ---- Pedido de retorno (H7)


async def test_pedido_de_retorno_usa_telefone_da_ligacao_e_e_idempotente(
    app_mcp: httpx.AsyncClient, http_api: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    telefone = "+5511900000006"
    chamada_id = await _nova_chamada(http_api, c, telefone)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        primeiro = await _ferramenta(cliente, "registrar_pedido_retorno", motivo="orçamento")
        segundo = await _ferramenta(cliente, "registrar_pedido_retorno", motivo="orçamento")
    assert primeiro == segundo
    assert primeiro["protocolo"].isdigit() and len(primeiro["protocolo"]) == 8
    async with fabrica() as sessao:
        pedidos = (
            await sessao.scalars(
                select(PedidoRetorno).where(PedidoRetorno.chamada_id == UUID(chamada_id))
            )
        ).all()
    assert [(p.telefone, p.motivo, p.status) for p in pedidos] == [
        (telefone, "orçamento", "pendente")
    ]


# ---- GET /clinicas


async def test_lista_as_clinicas_para_o_agente(http_api: httpx.AsyncClient, c: Cenario) -> None:
    corpo = (await http_api.get("/clinicas")).json()
    assert str(c.clinica_id) in [clinica["clinica_id"] for clinica in corpo["clinicas"]]


# ---- Consentimento (H1)


PROTEGIDAS: list[tuple[str, dict[str, Any]]] = [
    ("identificar_chamador", {}),
    ("selecionar_paciente", {"paciente_id": str(uuid4())}),
    ("cadastrar_paciente", {"nome": "Nova Pessoa", "data_nascimento": "1990-01-01"}),
    ("verificar_identidade", {"data_nascimento": "1990-01-01"}),
    (
        "segurar_horario",
        {"procedimento": "Limpeza", "dentista": "Ana", "inicio": "2031-01-06T14:00:00-03:00"},
    ),
    ("confirmar_agendamento", {"hold_id": str(uuid4())}),
    ("listar_meus_agendamentos", {}),
    ("remarcar_agendamento", {"agendamento_id": str(uuid4()), "hold_id": str(uuid4())}),
    ("cancelar_agendamento", {"agendamento_id": str(uuid4())}),
    ("registrar_pedido_retorno", {"motivo": "retorno"}),
]


@pytest.mark.parametrize(("ferramenta", "argumentos"), PROTEGIDAS)
@pytest.mark.parametrize("resposta", [None, False])
async def test_sem_consentimento_a_api_recusa(
    http_api: httpx.AsyncClient,
    app_mcp: httpx.AsyncClient,
    c: Cenario,
    ferramenta: str,
    argumentos: dict[str, Any],
    resposta: bool | None,
) -> None:
    chamada_id = await _nova_chamada(http_api, c, consentiu=False)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        if resposta is not None:
            await _ferramenta(cliente, "registrar_consentimento", aceitou=resposta)
        corpo = await _ferramenta(cliente, ferramenta, **argumentos)
    assert corpo == {"ok": False, "motivo": "consentimento_ausente"}


async def test_sem_consentimento_ainda_tira_duvidas(
    http_api: httpx.AsyncClient, app_mcp: httpx.AsyncClient, c: Cenario
) -> None:
    chamada_id = await _nova_chamada(http_api, c, consentiu=False)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        assert (await _ferramenta(cliente, "listar_procedimentos"))["ok"] is True


async def test_consentimento_e_recusa_ficam_na_chamada(
    http_api: httpx.AsyncClient, app_mcp: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    aceita = await _nova_chamada(http_api, c, consentiu=False)
    recusa = await _nova_chamada(http_api, c, consentiu=False)
    async with _na_chamada(app_mcp, aceita) as cliente:
        corpo = await _ferramenta(cliente, "registrar_consentimento", aceitou=True)
        assert corpo["consentimento"] is True
    async with _na_chamada(app_mcp, recusa) as cliente:
        corpo = await _ferramenta(cliente, "registrar_consentimento", aceitou=False)
        assert corpo["consentimento"] is False
    async with fabrica() as sessao:
        chamada_aceita = await sessao.get_one(Chamada, UUID(aceita))
        chamada_recusa = await sessao.get_one(Chamada, UUID(recusa))
    assert chamada_aceita.consentimento_em is not None
    assert chamada_aceita.consentimento_recusado_em is None
    assert chamada_recusa.consentimento_em is None
    assert chamada_recusa.consentimento_recusado_em is not None


async def test_recusa_vale_ate_o_fim_da_chamada(
    http_api: httpx.AsyncClient, app_mcp: httpx.AsyncClient, c: Cenario
) -> None:
    chamada_id = await _nova_chamada(http_api, c, consentiu=False)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        await _ferramenta(cliente, "registrar_consentimento", aceitou=False)
        corpo = await _ferramenta(cliente, "registrar_consentimento", aceitou=True)
        assert corpo["consentimento"] is False
        corpo = await _ferramenta(cliente, "identificar_chamador")
    assert corpo["motivo"] == "consentimento_ausente"


async def test_recusar_depois_de_aceitar_desfaz_a_selecao(
    http_api: httpx.AsyncClient, app_mcp: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        await _ferramenta(cliente, "selecionar_paciente", paciente_id=str(c.maria))
        await _ferramenta(cliente, "registrar_consentimento", aceitou=False)
    async with fabrica() as sessao:
        chamada = await sessao.get_one(Chamada, UUID(chamada_id))
    assert chamada.paciente_id is None
    assert chamada.consentimento_em is None


async def test_cadastro_por_voz_grava_o_consentimento(
    http_api: httpx.AsyncClient, app_mcp: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    chamada_id = await _nova_chamada(http_api, c)
    async with _na_chamada(app_mcp, chamada_id) as cliente:
        corpo = await _ferramenta(
            cliente, "cadastrar_paciente", nome="Nova Pessoa", data_nascimento="1990-01-01"
        )
    async with fabrica() as sessao:
        paciente = await sessao.get_one(Paciente, UUID(corpo["paciente_id"]))
    assert paciente.consentimento_em is not None
