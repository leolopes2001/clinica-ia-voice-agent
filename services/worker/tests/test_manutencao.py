from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_core.config import ConfigPostgres, ConfigRetencao
from clinica_core.db import criar_engine, criar_fabrica_sessoes
from clinica_core.modelos import (
    Agendamento,
    Chamada,
    Clinica,
    Consultorio,
    Dentista,
    Idempotencia,
    Paciente,
    PedidoRetorno,
    Procedimento,
)
from clinica_worker.manutencao import aplicar_retencao, processar_mensagem

RETENCAO = ConfigRetencao(transcricao_dias=90)
TELEFONE = "+5511900000001"


async def _agora(sessao: AsyncSession) -> datetime:
    return (await sessao.execute(select(func.now()))).scalar_one()


async def _clinica(sessao: AsyncSession) -> UUID:
    clinica = Clinica(nome="C", telefone="+5511", endereco="R", politica_cancelamento_horas=24)
    sessao.add(clinica)
    await sessao.flush()
    return clinica.id


async def _chamada(sessao: AsyncSession, clinica_id: UUID, **campos: Any) -> Chamada:
    chamada = Chamada(clinica_id=clinica_id, telefone=TELEFONE, sala=f"s-{uuid4()}", **campos)
    sessao.add(chamada)
    await sessao.flush()
    return chamada


async def _reler(sessao: AsyncSession, modelo: Any, id_: UUID) -> Any:
    return await sessao.get_one(modelo, id_, populate_existing=True)


async def test_transcricao_vencida_e_apagada_e_a_linha_fica(sessao: AsyncSession) -> None:
    clinica = await _clinica(sessao)
    agora = await _agora(sessao)
    vencida = await _chamada(
        sessao,
        clinica,
        transcricao="Paciente: tenho dor",
        apagar_em=agora - timedelta(minutes=1),
        fim=agora,
        desfecho="duvida",
    )
    valendo = await _chamada(
        sessao,
        clinica,
        transcricao="Paciente: oi",
        apagar_em=agora + timedelta(days=1),
        fim=agora,
        desfecho="duvida",
    )
    resultado = await aplicar_retencao(sessao, RETENCAO)
    assert resultado.transcricoes >= 1
    apagada = await _reler(sessao, Chamada, vencida.id)
    assert (apagada.transcricao, apagada.apagar_em) == (None, None)
    assert apagada.desfecho == "duvida"
    assert (await _reler(sessao, Chamada, valendo.id)).transcricao == "Paciente: oi"


async def test_telefone_some_no_prazo_mesmo_sem_transcricao(sessao: AsyncSession) -> None:
    clinica = await _clinica(sessao)
    agora = await _agora(sessao)
    antiga = await _chamada(
        sessao, clinica, inicio=agora - timedelta(days=91), fim=agora - timedelta(days=91)
    )
    recente = await _chamada(
        sessao, clinica, inicio=agora - timedelta(days=89), fim=agora - timedelta(days=89)
    )
    await aplicar_retencao(sessao, RETENCAO)
    linha = await _reler(sessao, Chamada, antiga.id)
    assert linha.telefone is None
    assert linha.inicio is not None
    assert (await _reler(sessao, Chamada, recente.id)).telefone == TELEFONE


async def test_chamada_que_nunca_recebeu_o_fim_tambem_vence(sessao: AsyncSession) -> None:
    clinica = await _clinica(sessao)
    agora = await _agora(sessao)
    orfa = await _chamada(sessao, clinica, inicio=agora - timedelta(days=91))
    await aplicar_retencao(sessao, RETENCAO)
    assert (await _reler(sessao, Chamada, orfa.id)).telefone is None


async def test_chamada_com_consulta_de_voz_perde_o_telefone_sem_quebrar_a_consulta(
    sessao: AsyncSession,
) -> None:
    clinica = await _clinica(sessao)
    agora = await _agora(sessao)
    paciente = Paciente(
        clinica_id=clinica,
        nome="Maria",
        telefone_e164=TELEFONE,
        data_nascimento=date(1990, 1, 1),
    )
    dentista = Dentista(clinica_id=clinica, nome="D")
    consultorio = Consultorio(clinica_id=clinica, nome="S")
    procedimento = Procedimento(clinica_id=clinica, nome="P", duracao_min=30)
    sessao.add_all([paciente, dentista, consultorio, procedimento])
    await sessao.flush()
    chamada = await _chamada(
        sessao,
        clinica,
        inicio=agora - timedelta(days=100),
        fim=agora - timedelta(days=100),
        apagar_em=agora - timedelta(days=10),
        transcricao="Paciente: marcar",
        paciente_id=paciente.id,
        identidade_verificada_em=agora - timedelta(days=100),
    )
    consulta = Agendamento(
        clinica_id=clinica,
        dentista_id=dentista.id,
        consultorio_id=consultorio.id,
        paciente_id=paciente.id,
        procedimento_id=procedimento.id,
        periodo=Range(agora - timedelta(days=99), agora - timedelta(days=99, minutes=-30)),
        status="concluido",
        chamada_id=chamada.id,
        origem="voz",
    )
    sessao.add(consulta)
    await sessao.flush()
    await aplicar_retencao(sessao, RETENCAO)
    linha = await _reler(sessao, Chamada, chamada.id)
    assert (linha.telefone, linha.transcricao, linha.paciente_id) == (None, None, None)
    assert linha.identidade_verificada_em is None
    assert (await _reler(sessao, Agendamento, consulta.id)).chamada_id == chamada.id
    # O cadastro do paciente não muda.
    assert (await _reler(sessao, Paciente, paciente.id)).telefone_e164 == TELEFONE


async def _pedido(sessao: AsyncSession, clinica: UUID, status: str, idade: int) -> PedidoRetorno:
    agora = await _agora(sessao)
    pedido = PedidoRetorno(
        clinica_id=clinica,
        protocolo=uuid4().hex[:8],
        telefone=TELEFONE,
        motivo="Orçamento",
        status=status,
        criado_em=agora - timedelta(days=idade),
    )
    sessao.add(pedido)
    await sessao.flush()
    return pedido


async def test_pedido_concluido_vence_e_o_pendente_fica(sessao: AsyncSession) -> None:
    clinica = await _clinica(sessao)
    concluido = await _pedido(sessao, clinica, "concluido", 91)
    pendente = await _pedido(sessao, clinica, "pendente", 91)
    recente = await _pedido(sessao, clinica, "concluido", 10)
    await aplicar_retencao(sessao, RETENCAO)
    apagado = await _reler(sessao, PedidoRetorno, concluido.id)
    assert (apagado.telefone, apagado.motivo) == (None, None)
    assert apagado.protocolo == concluido.protocolo
    assert (await _reler(sessao, PedidoRetorno, pendente.id)).telefone == TELEFONE
    assert (await _reler(sessao, PedidoRetorno, recente.id)).motivo == "Orçamento"


async def test_chave_de_idempotencia_vence_em_24_horas(sessao: AsyncSession) -> None:
    agora = await _agora(sessao)
    velha, nova = uuid4().hex, uuid4().hex
    sessao.add_all(
        [
            Idempotencia(chave=velha, resposta_json={}, criado_em=agora - timedelta(hours=25)),
            Idempotencia(chave=nova, resposta_json={}, criado_em=agora - timedelta(hours=23)),
        ]
    )
    await sessao.flush()
    await aplicar_retencao(sessao, RETENCAO)
    assert await sessao.get(Idempotencia, velha, populate_existing=True) is None
    assert await sessao.get(Idempotencia, nova, populate_existing=True) is not None


async def test_repetir_nao_muda_nada(sessao: AsyncSession) -> None:
    clinica = await _clinica(sessao)
    agora = await _agora(sessao)
    await _chamada(
        sessao,
        clinica,
        inicio=agora - timedelta(days=91),
        apagar_em=agora - timedelta(days=1),
        transcricao="x",
    )
    await aplicar_retencao(sessao, RETENCAO)
    segunda = await aplicar_retencao(sessao, RETENCAO)
    assert (segunda.transcricoes, segunda.telefones, segunda.pedidos) == (0, 0, 0)


class _Mensagem:
    body = b"{}"

    def __init__(self) -> None:
        self.resultado: str | None = None

    async def ack(self) -> None:
        self.resultado = "ack"

    async def reject(self, requeue: bool = False) -> None:
        self.resultado = f"reject requeue={requeue}"


@pytest.fixture
async def fabrica(banco: str) -> Any:
    engine = criar_engine(ConfigPostgres(db=banco))
    yield criar_fabrica_sessoes(engine)
    await engine.dispose()


async def test_mensagem_processada_confirma(fabrica: Any) -> None:
    mensagem = _Mensagem()
    await processar_mensagem(fabrica, RETENCAO, mensagem)  # type: ignore[arg-type]
    assert mensagem.resultado == "ack"


async def test_falha_vai_para_a_dlq_sem_voltar_a_fila() -> None:
    def fabrica_quebrada() -> Any:
        raise RuntimeError("sem banco")

    mensagem = _Mensagem()
    await processar_mensagem(fabrica_quebrada, RETENCAO, mensagem)  # type: ignore[arg-type]
    assert mensagem.resultado == "reject requeue=False"
