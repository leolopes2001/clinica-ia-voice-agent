from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_core.modelos import (
    Agendamento,
    Base,
    Chamada,
    Clinica,
    Consultorio,
    Dentista,
    Paciente,
    Procedimento,
)

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
SEGURADO_EM = datetime(2026, 12, 3, 14, tzinfo=UTC)
TABELAS = {
    "agendamentos",
    "auditoria",
    "bloqueios",
    "chamadas",
    "clinicas",
    "conhecimento_documentos",
    "conhecimento_trechos",
    "consultorios",
    "dentistas",
    "grade_trabalho",
    "idempotencia",
    "pacientes",
    "pedidos_retorno",
    "procedimentos",
    "usuarios",
}
TABELAS_SEM_CLINICA = {"clinicas", "idempotencia"}


@dataclass
class Cenario:
    clinica_id: UUID
    dentista_a: UUID
    dentista_b: UUID
    consultorio_a: UUID
    consultorio_b: UUID
    procedimento_id: UUID
    paciente_id: UUID


@pytest.fixture
async def cenario(sessao: AsyncSession) -> Cenario:
    clinica = Clinica(nome="C", telefone="+5511", endereco="R", politica_cancelamento_horas=24)
    sessao.add(clinica)
    await sessao.flush()
    dentista_a = Dentista(clinica_id=clinica.id, nome="A")
    dentista_b = Dentista(clinica_id=clinica.id, nome="B")
    consultorio_a = Consultorio(clinica_id=clinica.id, nome="1")
    consultorio_b = Consultorio(clinica_id=clinica.id, nome="2")
    procedimento = Procedimento(clinica_id=clinica.id, nome="Avaliação", duracao_min=30)
    paciente = _paciente(clinica.id)
    sessao.add_all([dentista_a, dentista_b, consultorio_a, consultorio_b, procedimento, paciente])
    await sessao.flush()
    return Cenario(
        clinica.id,
        dentista_a.id,
        dentista_b.id,
        consultorio_a.id,
        consultorio_b.id,
        procedimento.id,
        paciente.id,
    )


def _paciente(
    clinica_id: UUID,
    nome: str = "Maria",
    telefone: str = "+5511999990000",
    nascimento: date = date(1990, 1, 1),
) -> Paciente:
    return Paciente(
        clinica_id=clinica_id, nome=nome, telefone_e164=telefone, data_nascimento=nascimento
    )


def _agendamento(
    c: Cenario,
    inicio_min: int = 0,
    fim_min: int = 30,
    dentista: UUID | None = None,
    consultorio: UUID | None = None,
    **campos: Any,
) -> Agendamento:
    padrao: dict[str, Any] = {
        "status": "segurado",
        "expira_em": SEGURADO_EM + timedelta(minutes=3),
        "origem": "painel",
    }
    return Agendamento(
        clinica_id=c.clinica_id,
        dentista_id=dentista or c.dentista_a,
        consultorio_id=consultorio or c.consultorio_a,
        procedimento_id=c.procedimento_id,
        periodo=Range(
            SEGURADO_EM + timedelta(minutes=inicio_min),
            SEGURADO_EM + timedelta(minutes=fim_min),
            bounds="[)",
        ),
        **(padrao | campos),
    )


async def _gravar(sessao: AsyncSession, objeto: Any) -> None:
    async with sessao.begin_nested():
        sessao.add(objeto)
        await sessao.flush()


async def _espera_erro(sessao: AsyncSession, objeto: Any, sqlstate: str, constraint: str) -> None:
    with pytest.raises(DBAPIError) as erro:
        await _gravar(sessao, objeto)
    assert getattr(erro.value.orig, "sqlstate", None) == sqlstate
    assert constraint in str(erro.value.orig)


def test_upgrade_downgrade_upgrade_a_partir_de_banco_vazio(banco_vazio: str) -> None:
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")


def test_modelos_batem_com_a_migration(banco: str) -> None:
    command.check(Config(str(ALEMBIC_INI)))


def test_metadata_tem_as_tabelas_da_spec() -> None:
    assert set(Base.metadata.tables) == TABELAS


async def test_tabelas_extensoes_e_clinica_id(sessao: AsyncSession) -> None:
    tabelas = await sessao.scalars(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    )
    assert set(tabelas) == TABELAS | {"alembic_version"}

    extensoes = set(await sessao.scalars(text("SELECT extname FROM pg_extension")))
    assert {"btree_gist", "vector", "unaccent"} <= extensoes

    com_clinica = await sessao.scalars(
        text(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'clinica_id' AND is_nullable = 'NO'"
        )
    )
    assert set(com_clinica) == TABELAS - TABELAS_SEM_CLINICA


async def test_clinica_id_tem_fk(sessao: AsyncSession, cenario: Cenario) -> None:
    await _espera_erro(sessao, _paciente(uuid4()), "23503", "fk_pacientes_clinica_id_clinicas")


async def test_indices_do_rag(sessao: AsyncSession) -> None:
    linhas = await sessao.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'conhecimento_trechos'")
    )
    definicoes = {nome: definicao for nome, definicao in linhas}
    assert (
        "USING hnsw (embedding vector_cosine_ops)"
        in definicoes["ix_conhecimento_trechos_embedding"]
    )
    assert "USING gin (tsv)" in definicoes["ix_conhecimento_trechos_tsv"]


# Exclusion constraints


async def test_mesmo_dentista_com_horario_sobreposto_e_recusado(
    sessao: AsyncSession, cenario: Cenario
) -> None:
    await _gravar(sessao, _agendamento(cenario))
    await _espera_erro(
        sessao,
        _agendamento(cenario, 15, 45, consultorio=cenario.consultorio_b),
        "23P01",
        "sem_conflito_dentista",
    )


async def test_mesmo_consultorio_com_dentistas_diferentes_e_recusado(
    sessao: AsyncSession, cenario: Cenario
) -> None:
    await _gravar(sessao, _agendamento(cenario))
    await _espera_erro(
        sessao,
        _agendamento(cenario, 15, 45, dentista=cenario.dentista_b),
        "23P01",
        "sem_conflito_consultorio",
    )


async def test_confirmado_tambem_bloqueia(sessao: AsyncSession, cenario: Cenario) -> None:
    await _gravar(
        sessao,
        _agendamento(cenario, status="confirmado", expira_em=None, paciente_id=cenario.paciente_id),
    )
    await _espera_erro(
        sessao,
        _agendamento(cenario, consultorio=cenario.consultorio_b),
        "23P01",
        "sem_conflito_dentista",
    )


async def test_horarios_adjacentes_passam(sessao: AsyncSession, cenario: Cenario) -> None:
    await _gravar(sessao, _agendamento(cenario, 0, 30))
    await _gravar(sessao, _agendamento(cenario, 30, 60))


async def test_outro_dentista_e_consultorio_passam(sessao: AsyncSession, cenario: Cenario) -> None:
    await _gravar(sessao, _agendamento(cenario))
    await _gravar(
        sessao,
        _agendamento(cenario, dentista=cenario.dentista_b, consultorio=cenario.consultorio_b),
    )


@pytest.mark.parametrize("status", ["cancelado", "expirado", "faltou", "concluido"])
async def test_status_inativo_nao_bloqueia(
    sessao: AsyncSession, cenario: Cenario, status: str
) -> None:
    await _gravar(sessao, _agendamento(cenario, status=status, paciente_id=cenario.paciente_id))
    await _gravar(sessao, _agendamento(cenario))


# Demais regras de agendamentos


async def test_segurado_exige_expiracao(sessao: AsyncSession, cenario: Cenario) -> None:
    await _espera_erro(
        sessao, _agendamento(cenario, expira_em=None), "23514", "segurado_exige_expiracao"
    )


async def test_confirmado_exige_paciente(sessao: AsyncSession, cenario: Cenario) -> None:
    await _espera_erro(
        sessao,
        _agendamento(cenario, status="confirmado", expira_em=None),
        "23514",
        "confirmado_exige_paciente",
    )


async def test_voz_exige_chamada(sessao: AsyncSession, cenario: Cenario) -> None:
    await _espera_erro(sessao, _agendamento(cenario, origem="voz"), "23514", "voz_exige_chamada")


async def test_voz_com_chamada_e_painel_sem_chamada_passam(
    sessao: AsyncSession, cenario: Cenario
) -> None:
    chamada = Chamada(clinica_id=cenario.clinica_id, telefone="+55", sala="x")
    await _gravar(sessao, chamada)
    await _gravar(sessao, _agendamento(cenario, 0, 30, origem="voz", chamada_id=chamada.id))
    await _gravar(sessao, _agendamento(cenario, 30, 60, origem="painel"))


async def test_status_e_origem_invalidos(sessao: AsyncSession, cenario: Cenario) -> None:
    await _espera_erro(sessao, _agendamento(cenario, status="x"), "23514", "status_valido")
    await _espera_erro(sessao, _agendamento(cenario, origem="x"), "23514", "origem_valida")


async def test_chamada_id_tem_fk(sessao: AsyncSession, cenario: Cenario) -> None:
    await _espera_erro(
        sessao, _agendamento(cenario, chamada_id=uuid4()), "23503", "chamada_id_chamadas"
    )


async def test_motivo_de_cancelamento_e_gravado(sessao: AsyncSession, cenario: Cenario) -> None:
    agendamento = _agendamento(cenario, status="cancelado", motivo_cancelamento="viagem")
    await _gravar(sessao, agendamento)
    assert agendamento.motivo_cancelamento == "viagem"


# Vários pacientes por telefone


async def test_varios_pacientes_no_mesmo_telefone(sessao: AsyncSession, cenario: Cenario) -> None:
    await _gravar(sessao, _paciente(cenario.clinica_id, nome="Filho"))
    await _gravar(sessao, _paciente(cenario.clinica_id, nome="Filha"))
    await _gravar(sessao, _paciente(cenario.clinica_id, nascimento=date(2015, 5, 5)))


@pytest.mark.parametrize("nome", ["Maria", "MARIA", "  maria ", "Mária"])
async def test_mesma_pessoa_nao_e_duplicada(
    sessao: AsyncSession, cenario: Cenario, nome: str
) -> None:
    await _espera_erro(
        sessao, _paciente(cenario.clinica_id, nome=nome), "23505", "uq_pacientes_mesma_pessoa"
    )


async def test_mesma_pessoa_em_outro_telefone_passa(sessao: AsyncSession, cenario: Cenario) -> None:
    await _gravar(sessao, _paciente(cenario.clinica_id, telefone="+5511888880000"))


async def test_nome_normalizado_e_gerado(sessao: AsyncSession, cenario: Cenario) -> None:
    paciente = _paciente(cenario.clinica_id, nome="  João   da\tCONCEIÇÃO ")
    await _gravar(sessao, paciente)
    normalizado = await sessao.scalar(
        select(Paciente.nome_normalizado).where(Paciente.id == paciente.id)
    )
    assert normalizado == "joao da conceicao"


async def test_indice_por_clinica_e_telefone(sessao: AsyncSession) -> None:
    definicao = await sessao.scalar(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_pacientes_clinica_telefone'")
    )
    assert definicao is not None
    assert "(clinica_id, telefone_e164)" in definicao


async def test_chamada_guarda_o_paciente_escolhido(sessao: AsyncSession, cenario: Cenario) -> None:
    chamada = Chamada(
        clinica_id=cenario.clinica_id,
        telefone="+5511999990000",
        sala="call-1",
        paciente_id=cenario.paciente_id,
    )
    await _gravar(sessao, chamada)
    assert (
        await sessao.scalar(
            text("SELECT tentativas_identidade FROM chamadas WHERE id = :id"), {"id": chamada.id}
        )
        == 0
    )
    await _espera_erro(
        sessao,
        Chamada(clinica_id=cenario.clinica_id, telefone="+55", sala="x", paciente_id=uuid4()),
        "23503",
        "paciente_id_pacientes",
    )


# RAG


async def _criar_documento(sessao: AsyncSession, clinica_id: UUID) -> Any:
    return await sessao.scalar(
        text(
            "INSERT INTO conhecimento_documentos (clinica_id, titulo) "
            "VALUES (:clinica, 'Preços') RETURNING id"
        ),
        {"clinica": clinica_id},
    )


async def _inserir_trecho(
    sessao: AsyncSession, clinica_id: UUID, documento: Any, dimensao: int
) -> None:
    vetor = "[" + ",".join(["0.1"] * dimensao) + "]"
    await sessao.execute(
        text(
            "INSERT INTO conhecimento_trechos (clinica_id, documento_id, ordem, texto, embedding) "
            "VALUES (:clinica, :documento, 0, 'A limpeza dos dentes custa 100 reais', "
            "CAST(:vetor AS vector))"
        ),
        {"clinica": clinica_id, "documento": documento, "vetor": vetor},
    )


async def test_embedding_com_dimensao_errada_e_recusado(
    sessao: AsyncSession, cenario: Cenario
) -> None:
    documento = await _criar_documento(sessao, cenario.clinica_id)
    with pytest.raises(DBAPIError, match="expected 768 dimensions"):
        async with sessao.begin_nested():
            await _inserir_trecho(sessao, cenario.clinica_id, documento, 3)


async def test_tsv_e_gerado_e_pesquisavel(sessao: AsyncSession, cenario: Cenario) -> None:
    documento = await _criar_documento(sessao, cenario.clinica_id)
    await _inserir_trecho(sessao, cenario.clinica_id, documento, 768)
    # "dente" só casa com "dentes" pelo radical em português.
    encontrados = await sessao.scalar(
        text(
            "SELECT count(*) FROM conhecimento_trechos "
            "WHERE tsv @@ plainto_tsquery('portuguese', 'dente')"
        )
    )
    assert encontrados == 1
