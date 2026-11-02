from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_core.config import ConfigPostgres, ConfigRetencao
from clinica_core.db import criar_engine, criar_fabrica_sessoes
from clinica_core.mensageria import ChamadaEncerrada
from clinica_core.modelos import Chamada, Clinica
from clinica_worker.chamadas import gravar_encerramento, processar_mensagem


class _Mensagem:
    def __init__(self, corpo: bytes) -> None:
        self.body = corpo
        self.resultado: str | None = None

    async def ack(self) -> None:
        self.resultado = "ack"

    async def reject(self, requeue: bool = False) -> None:
        self.resultado = f"reject requeue={requeue}"


@pytest.fixture
async def fabrica(banco: str) -> AsyncIterator[object]:
    engine = criar_engine(ConfigPostgres(db=banco))
    yield criar_fabrica_sessoes(engine)
    await engine.dispose()


async def _chamada(sessao: AsyncSession, consentiu: bool) -> UUID:
    clinica = Clinica(nome="C", telefone="+5511", endereco="R", politica_cancelamento_horas=24)
    sessao.add(clinica)
    await sessao.flush()
    chamada = Chamada(
        clinica_id=clinica.id,
        telefone="+5511900000001",
        sala=f"call-{uuid4()}",
        consentimento_em=func.now() if consentiu else None,
    )
    sessao.add(chamada)
    await sessao.flush()
    return chamada.id


async def _ler(sessao: AsyncSession, chamada_id: UUID) -> Chamada:
    return await sessao.get_one(Chamada, chamada_id, populate_existing=True)


async def test_com_consentimento_guarda_transcricao_e_prazo(sessao: AsyncSession) -> None:
    chamada_id = await _chamada(sessao, consentiu=True)
    encerrada = ChamadaEncerrada(chamada_id=chamada_id, desfecho="agendou", transcricao="Oi")
    await gravar_encerramento(sessao, encerrada, ConfigRetencao(transcricao_dias=30))
    chamada = await _ler(sessao, chamada_id)
    assert (chamada.desfecho, chamada.transcricao) == ("agendou", "Oi")
    assert chamada.fim is not None
    assert chamada.apagar_em is not None
    assert (chamada.apagar_em - chamada.fim).days == 30


async def test_sem_consentimento_descarta_a_transcricao(sessao: AsyncSession) -> None:
    chamada_id = await _chamada(sessao, consentiu=False)
    encerrada = ChamadaEncerrada(chamada_id=chamada_id, desfecho="duvida", transcricao="Segredo")
    await gravar_encerramento(sessao, encerrada, ConfigRetencao())
    chamada = await _ler(sessao, chamada_id)
    assert chamada.transcricao is None
    assert chamada.apagar_em is None
    assert chamada.desfecho == "duvida"
    assert chamada.fim is not None


async def test_repetir_a_mensagem_nao_sobrescreve(sessao: AsyncSession) -> None:
    chamada_id = await _chamada(sessao, consentiu=True)
    retencao = ConfigRetencao()
    await gravar_encerramento(
        sessao, ChamadaEncerrada(chamada_id=chamada_id, desfecho="agendou"), retencao
    )
    await gravar_encerramento(
        sessao, ChamadaEncerrada(chamada_id=chamada_id, desfecho="abandonou"), retencao
    )
    assert (await _ler(sessao, chamada_id)).desfecho == "agendou"


async def test_mensagem_invalida_vai_para_a_dlq(fabrica: object) -> None:
    mensagem = _Mensagem(b"{}")
    await processar_mensagem(fabrica, ConfigRetencao(), mensagem)  # type: ignore[arg-type]
    assert mensagem.resultado == "reject requeue=False"


async def test_chamada_inexistente_nao_e_erro(fabrica: object) -> None:
    corpo = ChamadaEncerrada(chamada_id=uuid4(), desfecho="agendou").model_dump_json().encode()
    mensagem = _Mensagem(corpo)
    # Chamada inexistente não é erro: não há o que gravar.
    await processar_mensagem(fabrica, ConfigRetencao(), mensagem)  # type: ignore[arg-type]
    assert mensagem.resultado == "ack"
