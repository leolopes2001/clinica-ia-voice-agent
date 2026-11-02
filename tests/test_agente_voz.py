import asyncio
import json
import re
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from google.adk.events import Event
from google.adk.tools.base_tool import BaseTool
from google.genai import types

from clinica_agent.chamada import (
    ChamadaAberta,
    ClinicaNaoDefinida,
    SemTelefone,
    abrir_chamada,
    telefone_do_chamador,
)
from clinica_agent.config import ConfigAgente, ConfigLiveKit
from clinica_agent.navegador import criar_app as criar_app_navegador
from clinica_agent.recepcionista import (
    _esperar_antes,
    _executar_em_serie,
    cabecalhos_da_chamada,
    criar_agente,
    montar_prompt,
)
from clinica_agent.resumo import resumir

TELEFONE = "+5511900000001"


def _metadados(telefone: str = TELEFONE) -> str:
    return json.dumps({"telefone_de_teste": telefone})


def test_telefone_vem_do_sip_mesmo_com_metadados() -> None:
    atributos = {"sip.phoneNumber": "+5511988887777"}
    assert telefone_do_chamador(atributos, True, _metadados(), True) == "+5511988887777"


def test_telefone_de_teste_exige_permissao() -> None:
    assert telefone_do_chamador({}, False, _metadados(), True) == TELEFONE
    with pytest.raises(SemTelefone):
        telefone_do_chamador({}, False, _metadados(), False)


def test_navegador_nao_finge_ser_sip() -> None:
    with pytest.raises(SemTelefone):
        telefone_do_chamador({"sip.phoneNumber": TELEFONE}, False, None, True)


def _api(clinicas: list[dict[str, str]], pedidos: list[httpx.Request]) -> httpx.AsyncClient:
    def responder(pedido: httpx.Request) -> httpx.Response:
        pedidos.append(pedido)
        if pedido.method == "GET":
            return httpx.Response(200, json={"clinicas": clinicas})
        return httpx.Response(201, json={"chamada_id": "c1"})

    return httpx.AsyncClient(transport=httpx.MockTransport(responder), base_url="http://api")


async def test_abre_chamada_na_unica_clinica_com_a_sala_como_chave() -> None:
    clinica = str(uuid4())
    pedidos: list[httpx.Request] = []
    async with _api([{"clinica_id": clinica, "nome": "Exemplo"}], pedidos) as api:
        chamada = await abrir_chamada(api, None, TELEFONE, "sala-1")
    assert (chamada.chamada_id, chamada.clinica_nome) == ("c1", "Exemplo")
    assert pedidos[1].headers["Idempotency-Key"] == "sala-1"
    assert pedidos[1].headers["X-Clinica-Id"] == clinica
    assert json.loads(pedidos[1].content) == {"telefone": TELEFONE, "sala": "sala-1"}


@pytest.mark.parametrize("quantas", [0, 2])
async def test_sem_clinica_unica_nao_abre_chamada(quantas: int) -> None:
    clinicas = [{"clinica_id": str(uuid4()), "nome": "X"} for _ in range(quantas)]
    pedidos: list[httpx.Request] = []
    async with _api(clinicas, pedidos) as api:
        with pytest.raises(ClinicaNaoDefinida):
            await abrir_chamada(api, None, TELEFONE, "sala-1")
    assert len(pedidos) == 1


class _Contexto:
    def __init__(self, estado: dict[str, str]) -> None:
        self.state = estado


def test_cabecalho_da_chamada_vem_do_estado() -> None:
    contexto = _Contexto({"chamada_id": "abc"})
    assert cabecalhos_da_chamada(contexto) == {"X-Chamada-Id": "abc"}  # type: ignore[arg-type]


def test_prompt_traz_clinica_e_data_no_fuso_da_clinica() -> None:
    # 01h em UTC ainda é o dia anterior em São Paulo.
    texto = montar_prompt("Clínica X", datetime(2026, 11, 3, 1, tzinfo=UTC))
    assert "Clínica X" in texto
    assert "segunda-feira, 02/11/2026, 22:00" in texto


def test_modelo_vem_da_configuracao() -> None:
    config = ConfigAgente(modelo_voz="modelo-da-config")
    chamada = ChamadaAberta("c1", uuid4(), "Clínica X")
    agente = criar_agente(config, chamada)
    assert agente.model == "modelo-da-config"
    assert len(agente.tools) == 3


# ---- Prompt (T3.2)


@pytest.mark.parametrize(
    "regra",
    [
        "Você autoriza?",
        "registrar_consentimento",
        "explícito",
        "Nunca invente preço",
        "não dá diagnóstico",
        "no máximo 3",
        "acabou de ser ocupado",
        "não tem essa informação",
        "end_call",
    ],
)
def test_prompt_traz_as_regras(regra: str) -> None:
    assert regra in montar_prompt("Clínica X", datetime(2026, 11, 3, tzinfo=UTC))


def test_prompt_nao_deixa_marcador_sem_trocar() -> None:
    texto = montar_prompt("Clínica X", datetime(2026, 11, 3, tzinfo=UTC))
    assert "{" not in texto


# ---- Resumo da ligação


def _falou(texto: str) -> Event:
    return Event(author="user", input_transcription=types.Transcription(text=texto))


def _agente_falou(texto: str) -> Event:
    return Event(author="recepcionista", output_transcription=types.Transcription(text=texto))


def _resposta(nome: str, resultado: dict[str, Any]) -> Event:
    envelope = {"content": [{"type": "text", "text": json.dumps(resultado)}]}
    resposta = types.FunctionResponse(name=nome, response=envelope)
    return Event(
        author="recepcionista",
        content=types.Content(role="user", parts=[types.Part(function_response=resposta)]),
    )


CONSENTIU = _resposta("registrar_consentimento", {"ok": True, "consentimento": True})
RECUSOU = _resposta("registrar_consentimento", {"ok": True, "consentimento": False})


def test_resumo_com_consentimento_traz_a_transcricao_em_ordem() -> None:
    resumo = resumir([_agente_falou("Autoriza?"), _falou("Sim"), CONSENTIU])
    assert resumo.transcricao == "Recepcionista: Autoriza?\nPaciente: Sim"
    assert resumo.desfecho == "duvida"


@pytest.mark.parametrize("eventos", [[RECUSOU], [], [CONSENTIU, RECUSOU]])
def test_resumo_sem_consentimento_nao_leva_a_transcricao(eventos: list[Event]) -> None:
    assert resumir([_falou("Meu dente dói"), *eventos]).transcricao is None


def test_ninguem_falou_e_abandono() -> None:
    assert resumir([_agente_falou("Olá")]).desfecho == "abandonou"


@pytest.mark.parametrize(
    ("ferramenta", "desfecho"),
    [
        ("confirmar_agendamento", "agendou"),
        ("remarcar_agendamento", "remarcou"),
        ("cancelar_agendamento", "cancelou"),
    ],
)
def test_desfecho_vem_da_ferramenta_que_deu_certo(ferramenta: str, desfecho: str) -> None:
    assert resumir([_falou("oi"), _resposta(ferramenta, {"ok": True})]).desfecho == desfecho


def test_ferramenta_que_falhou_nao_vira_desfecho() -> None:
    falha = _resposta("confirmar_agendamento", {"ok": False, "motivo": "hold_perdido"})
    assert resumir([_falou("oi"), falha]).desfecho == "duvida"


# ---- Ferramentas


class _Ferramenta(BaseTool):
    def __init__(self, nome: str, registro: list[str], demora: float = 0.0) -> None:
        super().__init__(name=nome, description=nome)
        self.registro = registro
        self.demora = demora

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        self.registro.append(f"inicio {self.name}")
        await asyncio.sleep(self.demora)
        self.registro.append(f"fim {self.name}")
        return {"ok": True}


async def test_ferramentas_rodam_uma_por_vez_na_ordem_pedida() -> None:
    registro: list[str] = []
    trava = asyncio.Lock()
    verificar = _Ferramenta("verificar", registro, demora=0.01)
    listar = _Ferramenta("listar", registro)
    _executar_em_serie(verificar, trava)
    _executar_em_serie(listar, trava)
    await asyncio.gather(
        verificar.run_async(args={}, tool_context=None),
        listar.run_async(args={}, tool_context=None),
    )
    assert registro == ["inicio verificar", "fim verificar", "inicio listar", "fim listar"]


async def test_ferramenta_que_falha_libera_a_proxima() -> None:
    trava = asyncio.Lock()
    registro: list[str] = []

    class _Falha(_Ferramenta):
        async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
            raise RuntimeError("caiu")

    falha = _Falha("falha", registro)
    proxima = _Ferramenta("proxima", registro)
    _executar_em_serie(falha, trava)
    _executar_em_serie(proxima, trava)
    with pytest.raises(RuntimeError):
        await falha.run_async(args={}, tool_context=None)
    assert await proxima.run_async(args={}, tool_context=None) == {"ok": True}


async def test_desligar_espera_a_despedida() -> None:
    registro: list[str] = []
    desligar = _Ferramenta("end_call", registro)
    _esperar_antes(desligar, 0.02)
    tarefa = asyncio.create_task(desligar.run_async(args={}, tool_context=None))
    await asyncio.sleep(0)
    assert registro == []
    await tarefa
    assert registro == ["inicio end_call", "fim end_call"]


def test_prompt_proibe_inventar_dados() -> None:
    assert "Nunca invente o valor" in montar_prompt("X", datetime(2026, 11, 3, tzinfo=UTC))


# ---- Página de teste


@pytest.fixture
def navegador(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def despacho_falso(_: Any, telefone: str) -> tuple[str, str]:
        return "sala-1", f"token-{telefone}"

    monkeypatch.setattr("clinica_agent.navegador.despachar_chamada_de_teste", despacho_falso)
    return TestClient(criar_app_navegador(ConfigLiveKit(url_navegador="ws://localhost:7880")))


def test_pagina_de_teste_abre(navegador: TestClient) -> None:
    resposta = navegador.get("/")
    assert resposta.status_code == 200
    assert "Paciente novo" in resposta.text


def test_pagina_entrega_token_e_endereco_do_navegador(navegador: TestClient) -> None:
    corpo = navegador.post("/chamada", json={"telefone": TELEFONE}).json()
    assert corpo == {"url": "ws://localhost:7880", "token": f"token-{TELEFONE}", "sala": "sala-1"}


def test_pagina_recusa_telefone_invalido(navegador: TestClient) -> None:
    assert navegador.post("/chamada", json={"telefone": "123"}).status_code == 422


def test_pagina_so_usa_telefones_do_seed() -> None:
    pagina = files("clinica_agent").joinpath("navegador.html").read_text("utf-8")
    for telefone in re.findall(r"\+55\d{10,11}", pagina):
        assert telefone.startswith("+55119000000"), telefone
