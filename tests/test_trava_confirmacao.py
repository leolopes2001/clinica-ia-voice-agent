import asyncio
from typing import Any

import pytest
from google.adk.events import Event
from google.adk.tools.base_tool import BaseTool
from google.genai import types

from clinica_agent.trava import TravaDeConfirmacao, confirmacao_explicita

CONSULTA = {"agendamento_id": "a1"}
TROCA = {"agendamento_id": "a1", "hold_id": "h1"}
OK = {"content": [{"type": "text", "text": '{"ok": true}'}]}


class _Ferramenta(BaseTool):
    def __init__(self, nome: str) -> None:
        super().__init__(name=nome, description=nome)


PREPARAR = _Ferramenta("preparar_cancelamento")
EXECUTAR = _Ferramenta("executar_cancelamento")
PREPARAR_TROCA = _Ferramenta("preparar_remarcacao")
EXECUTAR_TROCA = _Ferramenta("executar_remarcacao")


def _fala(texto: str, finished: bool = True) -> Event:
    return Event(
        author="user",
        input_transcription=types.Transcription(text=texto, finished=finished),
        partial=not finished,
    )


def _trava() -> TravaDeConfirmacao:
    return TravaDeConfirmacao(espera_s=0.05, assentar_s=0.01)


async def _preparada(args: dict[str, Any] = CONSULTA) -> TravaDeConfirmacao:
    trava = _trava()
    await trava.depois_da_ferramenta(PREPARAR, args, None, OK)
    await asyncio.sleep(0.001)
    return trava


@pytest.mark.parametrize(
    "fala",
    [
        "Sim",
        "Sim.",
        "Sim, pode cancelar.",
        "SIM, PODE TROCAR",
        "Pode cancelar.",
        "pode confirmar",
        "Confirmo.",
        "sim senhora",
    ],
)
def test_confirmacoes_explicitas(fala: str) -> None:
    assert confirmacao_explicita(fala)


@pytest.mark.parametrize(
    "fala",
    [
        "Essa mesma",
        "É essa mesma.",
        "Pode ser",
        "Pode ser, sim",
        "a primeira",
        "ok",
        "isso",
        "Não",
        "Sim, mas espera",
        "Sim, não sei",
        "Não pode cancelar",
        "pode cancelar não",
        "Assim fica melhor",
        "Sim?",
        "Talvez sim",
        "acho que sim",
        "",
    ],
)
def test_nao_sao_confirmacao_explicita(fala: str) -> None:
    assert not confirmacao_explicita(fala)


async def test_ferramentas_que_nao_executam_passam_sem_fala() -> None:
    assert await _trava().antes_da_ferramenta(PREPARAR, CONSULTA, None) is None


async def test_executar_sem_preparar_e_bloqueado() -> None:
    trava = _trava()
    trava.observar(_fala("Sim, pode cancelar"))
    resposta = await trava.antes_da_ferramenta(EXECUTAR, CONSULTA, None)
    assert resposta == {"ok": False, "motivo": "preparo_ausente"}


async def test_preparo_que_falhou_nao_vale() -> None:
    trava = _trava()
    falha = {"content": [{"type": "text", "text": '{"ok": false, "motivo": "x"}'}]}
    await trava.depois_da_ferramenta(PREPARAR, CONSULTA, None, falha)
    trava.observar(_fala("Sim"))
    resposta = await trava.antes_da_ferramenta(EXECUTAR, CONSULTA, None)
    assert resposta == {"ok": False, "motivo": "preparo_ausente"}


async def test_preparo_de_outra_consulta_nao_vale() -> None:
    trava = await _preparada({"agendamento_id": "outra"})
    trava.observar(_fala("Sim"))
    resposta = await trava.antes_da_ferramenta(EXECUTAR, CONSULTA, None)
    assert resposta == {"ok": False, "motivo": "preparo_ausente"}


async def test_preparo_de_cancelar_nao_vale_para_remarcar() -> None:
    trava = await _preparada(TROCA)
    trava.observar(_fala("Sim"))
    resposta = await trava.antes_da_ferramenta(EXECUTAR_TROCA, TROCA, None)
    assert resposta == {"ok": False, "motivo": "preparo_ausente"}


async def test_sim_antes_de_preparar_nao_vale() -> None:
    trava = _trava()
    trava.observar(_fala("Sim, autorizo"))
    await asyncio.sleep(0.001)
    await trava.depois_da_ferramenta(PREPARAR, CONSULTA, None, OK)
    resposta = await trava.antes_da_ferramenta(EXECUTAR, CONSULTA, None)
    assert resposta == {"ok": False, "motivo": "confirmacao_ausente"}


async def test_essa_mesma_nao_executa() -> None:
    trava = await _preparada()
    trava.observar(_fala("Essa mesma"))
    resposta = await trava.antes_da_ferramenta(EXECUTAR, CONSULTA, None)
    assert resposta == {"ok": False, "motivo": "confirmacao_ausente"}


async def test_sim_depois_de_preparar_executa() -> None:
    trava = await _preparada()
    trava.observar(_fala("Sim, pode cancelar"))
    assert await trava.antes_da_ferramenta(EXECUTAR, CONSULTA, None) is None


async def test_vale_a_ultima_fala_e_nao_uma_anterior() -> None:
    trava = await _preparada()
    trava.observar(_fala("Sim"))
    trava.observar(_fala("Espera, deixa eu ver"))
    resposta = await trava.antes_da_ferramenta(EXECUTAR, CONSULTA, None)
    assert resposta == {"ok": False, "motivo": "confirmacao_ausente"}


async def test_transcricao_parcial_conta_antes_da_final() -> None:
    trava = await _preparada()
    trava.observar(_fala("Sim, pode", finished=False))
    trava.observar(_fala(" cancelar", finished=False))
    assert await trava.antes_da_ferramenta(EXECUTAR, CONSULTA, None) is None


async def test_espera_a_transcricao_que_chega_depois_da_ferramenta() -> None:
    trava = await _preparada()
    tarefa = asyncio.create_task(trava.antes_da_ferramenta(EXECUTAR, CONSULTA, None))
    await asyncio.sleep(0.005)
    assert not tarefa.done()
    trava.observar(_fala("Sim"))
    assert await tarefa is None


async def test_resto_da_fala_que_chega_depois_desfaz_o_sim() -> None:
    trava = TravaDeConfirmacao(espera_s=0.1, assentar_s=0.03)
    await trava.depois_da_ferramenta(PREPARAR, CONSULTA, None, OK)
    await asyncio.sleep(0.001)
    trava.observar(_fala("Sim,", finished=False))
    tarefa = asyncio.create_task(trava.antes_da_ferramenta(EXECUTAR, CONSULTA, None))
    await asyncio.sleep(0.005)
    trava.observar(_fala(" mas espera", finished=False))
    assert await tarefa == {"ok": False, "motivo": "confirmacao_ausente"}


async def test_executar_com_sucesso_gasta_o_preparo() -> None:
    trava = await _preparada()
    trava.observar(_fala("Sim"))
    assert await trava.antes_da_ferramenta(EXECUTAR, CONSULTA, None) is None
    await trava.depois_da_ferramenta(EXECUTAR, CONSULTA, None, OK)
    resposta = await trava.antes_da_ferramenta(EXECUTAR, CONSULTA, None)
    assert resposta == {"ok": False, "motivo": "preparo_ausente"}


async def test_remarcar_exige_o_mesmo_horario_preparado() -> None:
    trava = _trava()
    await trava.depois_da_ferramenta(PREPARAR_TROCA, TROCA, None, OK)
    await asyncio.sleep(0.001)
    trava.observar(_fala("Sim, pode trocar"))
    assert await trava.antes_da_ferramenta(EXECUTAR_TROCA, TROCA, None) is None
    outro = {"agendamento_id": "a1", "hold_id": "h2"}
    resposta = await trava.antes_da_ferramenta(EXECUTAR_TROCA, outro, None)
    assert resposta == {"ok": False, "motivo": "preparo_ausente"}


async def test_recusa_da_trava_nao_gasta_o_preparo() -> None:
    trava = await _preparada()
    trava.observar(_fala("Pode ser"))
    recusa = await trava.antes_da_ferramenta(EXECUTAR, CONSULTA, None)
    assert recusa == {"ok": False, "motivo": "confirmacao_ausente"}
    await trava.depois_da_ferramenta(EXECUTAR, CONSULTA, None, recusa)
    await asyncio.sleep(0.001)
    trava.observar(_fala("Sim, pode cancelar"))
    assert await trava.antes_da_ferramenta(EXECUTAR, CONSULTA, None) is None


async def test_falha_real_ao_executar_gasta_o_preparo() -> None:
    trava = await _preparada()
    trava.observar(_fala("Sim"))
    falha = {"content": [{"type": "text", "text": '{"ok": false, "motivo": "hold_perdido"}'}]}
    await trava.depois_da_ferramenta(EXECUTAR, CONSULTA, None, falha)
    resposta = await trava.antes_da_ferramenta(EXECUTAR, CONSULTA, None)
    assert resposta == {"ok": False, "motivo": "preparo_ausente"}
