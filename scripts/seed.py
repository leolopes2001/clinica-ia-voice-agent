"""Dados fictícios de exemplo: `uv run python scripts/seed.py`. Pode rodar mais de uma vez.

Com o Compose no ar, também copia a base de exemplo para o volume do worker e pede a indexação.
"""

import argparse
import asyncio
import subprocess
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_core.config import ConfigPostgres, ConfigRabbit
from clinica_core.db import criar_engine, criar_fabrica_sessoes
from clinica_core.mensageria import (
    FILA_CONHECIMENTO_INDEXAR,
    IndexarDocumento,
    abrir_canal,
    conectar,
    declarar_topologia,
    publicar,
)
from clinica_core.modelos import (
    Base,
    Bloqueio,
    Clinica,
    ConhecimentoDocumento,
    Consultorio,
    Dentista,
    GradeTrabalho,
    Paciente,
    Procedimento,
)

RAIZ = Path(__file__).resolve().parent.parent
BASE = RAIZ / "exemplos" / "conhecimento"
FUSO = ZoneInfo("America/Sao_Paulo")
DIAS_DE_ALMOCO = 90
HELENA = "Dra. Helena Prado"
RAFAEL = "Dr. Rafael Nunes"
PROCEDIMENTOS: list[tuple[str, list[str], int, int, list[str]]] = [
    ("Avaliação", ["consulta", "primeira consulta"], 30, 10, [HELENA, RAFAEL]),
    ("Limpeza", ["profilaxia", "tartarectomia"], 45, 15, [HELENA]),
    ("Restauração", ["obturação", "resina"], 60, 15, [HELENA]),
    ("Clareamento", ["clareamento a laser", "branqueamento"], 60, 15, [HELENA]),
    ("Canal", ["endodontia", "tratamento de canal"], 90, 15, [RAFAEL]),
    ("Extração", ["extrair dente", "arrancar dente"], 60, 15, [RAFAEL]),
    ("Extração de siso", ["tirar o siso", "siso"], 90, 15, [RAFAEL]),
    ("Manutenção de aparelho", ["aparelho", "ortodontia"], 30, 10, [RAFAEL]),
]
# Telefones fictícios: os reais ficam só no .env, nunca aqui.
PACIENTES = [
    ("Maria Oliveira", "+5511900000001", date(1985, 3, 12)),
    ("Carla Mendes", "+5511900000002", date(1980, 7, 25)),
    ("Bruno Mendes", "+5511900000002", date(2014, 11, 3)),
]
_ALVO_DO_DOCUMENTO = "/dados/conhecimento/exemplo"


async def _obter[M: Base](
    sessao: AsyncSession, modelo: type[M], *filtros: Any, **valores: Any
) -> M:
    existente = await sessao.scalar(select(modelo).where(and_(*filtros)))
    if existente is not None:
        return existente
    novo = modelo(**valores)
    sessao.add(novo)
    await sessao.flush()
    return novo


async def _povoar(sessao: AsyncSession) -> tuple[UUID, list[UUID]]:
    clinica = await _obter(
        sessao,
        Clinica,
        Clinica.nome == "Clínica Exemplo Sorriso",
        nome="Clínica Exemplo Sorriso",
        telefone="+551130000000",
        endereco="Rua das Palmeiras Fictícias, 100, sala 12, Bairro Modelo, Cidade Exemplo - SP",
        politica_cancelamento_horas=24,
    )
    cid = clinica.id
    dentistas = {
        nome: await _obter(
            sessao,
            Dentista,
            Dentista.clinica_id == cid,
            Dentista.nome == nome,
            clinica_id=cid,
            nome=nome,
            especialidades=especialidades,
        )
        for nome, especialidades in (
            (HELENA, ["clínica geral"]),
            (RAFAEL, ["endodontia", "ortodontia"]),
        )
    }
    consultorios = [
        await _obter(
            sessao,
            Consultorio,
            Consultorio.clinica_id == cid,
            Consultorio.nome == nome,
            clinica_id=cid,
            nome=nome,
        )
        for nome in ("Consultório 1", "Consultório 2")
    ]
    for nome, sinonimos, duracao, limpeza, habilitados in PROCEDIMENTOS:
        await _obter(
            sessao,
            Procedimento,
            Procedimento.clinica_id == cid,
            Procedimento.nome == nome,
            clinica_id=cid,
            nome=nome,
            sinonimos=sinonimos,
            duracao_min=duracao,
            intervalo_limpeza_min=limpeza,
            dentistas_habilitados=[dentistas[d].id for d in habilitados],
        )

    # Dra. Helena de segunda a sexta; Dr. Rafael de terça a sábado, com o sábado só de manhã.
    grades = [(HELENA, consultorios[0], dia, time(19)) for dia in range(5)]
    grades += [(RAFAEL, consultorios[1], dia, time(19)) for dia in range(1, 5)]
    grades += [(RAFAEL, consultorios[1], 5, time(12))]
    for nome, consultorio, dia, fim in grades:
        dentista_id = dentistas[nome].id
        await _obter(
            sessao,
            GradeTrabalho,
            GradeTrabalho.dentista_id == dentista_id,
            GradeTrabalho.dia_semana == dia,
            clinica_id=cid,
            dentista_id=dentista_id,
            consultorio_id=consultorio.id,
            dia_semana=dia,
            inicio=time(8),
            fim=fim,
        )

    # Os bloqueios não são recorrentes: um por dia útil, e rodar o seed de novo estende a janela.
    hoje = datetime.now(UTC).astimezone(FUSO).date()
    for deslocamento in range(DIAS_DE_ALMOCO):
        dia_almoco = hoje + timedelta(days=deslocamento)
        if dia_almoco.weekday() >= 5:
            continue
        periodo = Range(
            datetime.combine(dia_almoco, time(12), tzinfo=FUSO),
            datetime.combine(dia_almoco, time(13), tzinfo=FUSO),
        )
        await _obter(
            sessao,
            Bloqueio,
            Bloqueio.clinica_id == cid,
            Bloqueio.motivo == "Almoço",
            Bloqueio.periodo == periodo,
            clinica_id=cid,
            periodo=periodo,
            motivo="Almoço",
        )

    for nome, telefone, nascimento in PACIENTES:
        await _obter(
            sessao,
            Paciente,
            Paciente.clinica_id == cid,
            Paciente.telefone_e164 == telefone,
            Paciente.nome == nome,
            clinica_id=cid,
            nome=nome,
            telefone_e164=telefone,
            data_nascimento=nascimento,
        )

    documentos = [
        await _obter(
            sessao,
            ConhecimentoDocumento,
            ConhecimentoDocumento.clinica_id == cid,
            ConhecimentoDocumento.arquivo == f"exemplo/{arquivo.name}",
            clinica_id=cid,
            # O título é a fonte que a busca devolve: vem do próprio documento.
            titulo=arquivo.read_text(encoding="utf-8").splitlines()[0].lstrip("# "),
            arquivo=f"exemplo/{arquivo.name}",
        )
        for arquivo in sorted(BASE.glob("*.md"))
    ]
    return cid, [documento.id for documento in documentos]


def _copiar_base() -> None:
    subprocess.run(
        ["docker", "compose", "cp", f"{BASE}/.", f"worker:{_ALVO_DO_DOCUMENTO}/"],
        cwd=RAIZ,
        check=True,
        capture_output=True,
    )


async def _pedir_indexacao(documentos: list[UUID]) -> None:
    conexao = await conectar(ConfigRabbit())
    try:
        canal = await abrir_canal(conexao)
        await declarar_topologia(canal)
        for documento_id in documentos:
            await publicar(
                canal, FILA_CONHECIMENTO_INDEXAR, IndexarDocumento(documento_id=documento_id)
            )
    finally:
        await conexao.close()


async def _principal(sem_conhecimento: bool) -> None:
    engine = criar_engine(ConfigPostgres())
    try:
        async with criar_fabrica_sessoes(engine)() as sessao:
            clinica_id, documentos = await _povoar(sessao)
            await sessao.commit()
    finally:
        await engine.dispose()
    if not sem_conhecimento:
        await asyncio.to_thread(_copiar_base)
        await _pedir_indexacao(documentos)
        print(f"{len(documentos)} documentos enviados para indexação.")
    print(f"CLINICA_ID={clinica_id}")


if __name__ == "__main__":
    argumentos = argparse.ArgumentParser(description=__doc__)
    argumentos.add_argument(
        "--sem-conhecimento", action="store_true", help="Não copia nem indexa a base de exemplo."
    )
    asyncio.run(_principal(argumentos.parse_args().sem_conhecimento))
