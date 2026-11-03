"""Dentistas, consultórios, procedimentos, grade e bloqueios. Só para administradores."""

from collections.abc import Iterable
from datetime import datetime, time
from http import HTTPStatus
from typing import Annotated, Any, Self
from uuid import UUID

from fastapi import APIRouter
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_api import auditoria
from clinica_api.agenda import ErroAgenda
from clinica_api.autenticacao import Admin, Logado, Sessao, UsuarioAtual
from clinica_api.esquemas import _Entrada
from clinica_core.modelos import (
    Base,
    Bloqueio,
    Consultorio,
    Dentista,
    GradeTrabalho,
    Procedimento,
)

router = APIRouter(prefix="/painel")

_Nome = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
_SQLSTATE_EXCLUSAO = "23P01"
_SQLSTATE_FK = "23503"


class DentistaEntrada(_Entrada):
    nome: _Nome
    especialidades: list[_Nome] = []
    ativo: bool = True


class ConsultorioEntrada(_Entrada):
    nome: _Nome
    ativo: bool = True


class ConsultorioResumo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    nome: str
    ativo: bool


class ProcedimentoEntrada(_Entrada):
    nome: _Nome
    sinonimos: list[_Nome] = []
    duracao_min: int = Field(gt=0, le=720)
    intervalo_limpeza_min: int = Field(default=0, ge=0, le=240)
    dentistas_habilitados: list[UUID] = []


class GradeEntrada(_Entrada):
    dentista_id: UUID
    consultorio_id: UUID
    dia_semana: int = Field(ge=0, le=6)
    inicio: time
    fim: time

    @model_validator(mode="after")
    def _inicio_antes_do_fim(self) -> Self:
        if self.inicio >= self.fim:
            raise ValueError("inicio precisa vir antes de fim")
        return self


class GradeResumo(BaseModel):
    id: UUID
    dentista_id: UUID
    dentista: str
    consultorio_id: UUID
    consultorio: str
    dia_semana: int
    inicio: time
    fim: time


class BloqueioEntrada(_Entrada):
    # Vazio: a clínica toda.
    dentista_id: UUID | None = None
    inicio: AwareDatetime
    fim: AwareDatetime
    motivo: _Nome

    @model_validator(mode="after")
    def _inicio_antes_do_fim(self) -> Self:
        if self.inicio >= self.fim:
            raise ValueError("inicio precisa vir antes de fim")
        return self


class BloqueioResumo(BaseModel):
    id: UUID
    dentista_id: UUID | None
    dentista: str | None
    inicio: datetime
    fim: datetime
    motivo: str


def _para_json(valores: dict[str, Any]) -> dict[str, Any]:
    def simples(valor: Any) -> Any:
        if isinstance(valor, list):
            return [simples(item) for item in valor]
        return valor if isinstance(valor, (bool, int, type(None))) else str(valor)

    return {chave: simples(valor) for chave, valor in valores.items()}


async def _gravar(sessao: AsyncSession) -> None:
    try:
        await sessao.flush()
    except IntegrityError as erro:
        await sessao.rollback()
        estado = getattr(erro.orig, "sqlstate", None)
        if estado == _SQLSTATE_EXCLUSAO:
            raise ErroAgenda("grade_sobreposta") from erro
        if estado == _SQLSTATE_FK:
            raise ErroAgenda("em_uso") from erro
        raise


async def _da_clinica(sessao: AsyncSession, modelo: Any, id_: UUID, atual: UsuarioAtual) -> Any:
    registro = await sessao.scalar(
        select(modelo).where(modelo.id == id_, modelo.clinica_id == atual.clinica_id)
    )
    if registro is None:
        raise ErroAgenda("registro_nao_encontrado")
    return registro


async def _conferir_referencias(
    sessao: AsyncSession, modelo: Any, ids: Iterable[UUID], atual: UsuarioAtual
) -> None:
    """A FK não sabe de clínica: sem isso, daria para apontar para o dentista de outra."""
    procurados = set(ids)
    if not procurados:
        return
    achados = await sessao.scalar(
        select(func.count())
        .select_from(modelo)
        .where(modelo.id.in_(procurados), modelo.clinica_id == atual.clinica_id)
    )
    if achados != len(procurados):
        raise ErroAgenda("registro_nao_encontrado")


def _auditar(
    sessao: AsyncSession,
    atual: UsuarioAtual,
    acao: str,
    entidade: str,
    id_: UUID,
    antes: dict[str, Any] | None = None,
    depois: dict[str, Any] | None = None,
) -> None:
    auditoria.registrar(
        sessao,
        atual.clinica_id,
        atual.id,
        acao,
        entidade,
        antes={"id": str(id_), **_para_json(antes)} if antes is not None else None,
        depois={"id": str(id_), **_para_json(depois)} if depois is not None else None,
    )


def _colunas(registro: Base, campos: Iterable[str]) -> dict[str, Any]:
    return {campo: getattr(registro, campo) for campo in campos}


async def _criar(
    sessao: AsyncSession, atual: UsuarioAtual, modelo: Any, entidade: str, dados: dict[str, Any]
) -> Any:
    registro = modelo(clinica_id=atual.clinica_id, **dados)
    sessao.add(registro)
    await _gravar(sessao)
    _auditar(sessao, atual, "criar", entidade, registro.id, depois=dados)
    return registro


async def _atualizar(
    sessao: AsyncSession,
    atual: UsuarioAtual,
    modelo: Any,
    entidade: str,
    id_: UUID,
    dados: dict[str, Any],
) -> Any:
    registro = await _da_clinica(sessao, modelo, id_, atual)
    antes = _colunas(registro, dados)
    for campo, valor in dados.items():
        setattr(registro, campo, valor)
    await _gravar(sessao)
    _auditar(sessao, atual, "atualizar", entidade, id_, antes=antes, depois=dados)
    return registro


async def _apagar(
    sessao: AsyncSession, atual: UsuarioAtual, modelo: Any, entidade: str, id_: UUID
) -> None:
    registro = await _da_clinica(sessao, modelo, id_, atual)
    await sessao.delete(registro)
    await _gravar(sessao)
    _auditar(sessao, atual, "apagar", entidade, id_)
    await sessao.commit()


def _dentista_resumo(dentista: Dentista) -> dict[str, Any]:
    return {
        "id": dentista.id,
        "nome": dentista.nome,
        "especialidades": dentista.especialidades,
        "ativo": dentista.ativo,
    }


def _procedimento_resumo(procedimento: Procedimento) -> dict[str, Any]:
    return {
        "id": procedimento.id,
        "nome": procedimento.nome,
        "sinonimos": procedimento.sinonimos,
        "duracao_min": procedimento.duracao_min,
        "intervalo_limpeza_min": procedimento.intervalo_limpeza_min,
        "dentistas_habilitados": procedimento.dentistas_habilitados,
    }


@router.post("/dentistas", status_code=HTTPStatus.CREATED)
async def criar_dentista(entrada: DentistaEntrada, sessao: Sessao, atual: Admin) -> dict[str, Any]:
    dentista = await _criar(sessao, atual, Dentista, "dentista", entrada.model_dump())
    resposta = _dentista_resumo(dentista)
    await sessao.commit()
    return resposta


@router.put("/dentistas/{id_}")
async def atualizar_dentista(
    id_: UUID, entrada: DentistaEntrada, sessao: Sessao, atual: Admin
) -> dict[str, Any]:
    dentista = await _atualizar(sessao, atual, Dentista, "dentista", id_, entrada.model_dump())
    resposta = _dentista_resumo(dentista)
    await sessao.commit()
    return resposta


@router.delete("/dentistas/{id_}", status_code=HTTPStatus.NO_CONTENT)
async def apagar_dentista(id_: UUID, sessao: Sessao, atual: Admin) -> None:
    await _apagar(sessao, atual, Dentista, "dentista", id_)


@router.get("/consultorios")
async def listar_consultorios(sessao: Sessao, atual: Logado) -> list[ConsultorioResumo]:
    consultorios = await sessao.scalars(
        select(Consultorio)
        .where(Consultorio.clinica_id == atual.clinica_id)
        .order_by(Consultorio.nome)
    )
    return [ConsultorioResumo.model_validate(c) for c in consultorios]


@router.post("/consultorios", status_code=HTTPStatus.CREATED)
async def criar_consultorio(
    entrada: ConsultorioEntrada, sessao: Sessao, atual: Admin
) -> ConsultorioResumo:
    consultorio = await _criar(sessao, atual, Consultorio, "consultorio", entrada.model_dump())
    resposta = ConsultorioResumo.model_validate(consultorio)
    await sessao.commit()
    return resposta


@router.put("/consultorios/{id_}")
async def atualizar_consultorio(
    id_: UUID, entrada: ConsultorioEntrada, sessao: Sessao, atual: Admin
) -> ConsultorioResumo:
    consultorio = await _atualizar(
        sessao, atual, Consultorio, "consultorio", id_, entrada.model_dump()
    )
    resposta = ConsultorioResumo.model_validate(consultorio)
    await sessao.commit()
    return resposta


@router.delete("/consultorios/{id_}", status_code=HTTPStatus.NO_CONTENT)
async def apagar_consultorio(id_: UUID, sessao: Sessao, atual: Admin) -> None:
    await _apagar(sessao, atual, Consultorio, "consultorio", id_)


@router.post("/procedimentos", status_code=HTTPStatus.CREATED)
async def criar_procedimento(
    entrada: ProcedimentoEntrada, sessao: Sessao, atual: Admin
) -> dict[str, Any]:
    await _conferir_referencias(sessao, Dentista, entrada.dentistas_habilitados, atual)
    procedimento = await _criar(sessao, atual, Procedimento, "procedimento", entrada.model_dump())
    resposta = _procedimento_resumo(procedimento)
    await sessao.commit()
    return resposta


@router.put("/procedimentos/{id_}")
async def atualizar_procedimento(
    id_: UUID, entrada: ProcedimentoEntrada, sessao: Sessao, atual: Admin
) -> dict[str, Any]:
    await _conferir_referencias(sessao, Dentista, entrada.dentistas_habilitados, atual)
    procedimento = await _atualizar(
        sessao, atual, Procedimento, "procedimento", id_, entrada.model_dump()
    )
    resposta = _procedimento_resumo(procedimento)
    await sessao.commit()
    return resposta


@router.delete("/procedimentos/{id_}", status_code=HTTPStatus.NO_CONTENT)
async def apagar_procedimento(id_: UUID, sessao: Sessao, atual: Admin) -> None:
    await _apagar(sessao, atual, Procedimento, "procedimento", id_)


async def _grade_resumo(sessao: AsyncSession, grade: GradeTrabalho) -> GradeResumo:
    dentista = await sessao.get_one(Dentista, grade.dentista_id)
    consultorio = await sessao.get_one(Consultorio, grade.consultorio_id)
    return GradeResumo(
        id=grade.id,
        dentista_id=grade.dentista_id,
        dentista=dentista.nome,
        consultorio_id=grade.consultorio_id,
        consultorio=consultorio.nome,
        dia_semana=grade.dia_semana,
        inicio=grade.inicio,
        fim=grade.fim,
    )


@router.get("/grade")
async def listar_grade(sessao: Sessao, atual: Admin) -> list[GradeResumo]:
    linhas = await sessao.execute(
        select(GradeTrabalho, Dentista.nome, Consultorio.nome)
        .join(Dentista, GradeTrabalho.dentista_id == Dentista.id)
        .join(Consultorio, GradeTrabalho.consultorio_id == Consultorio.id)
        .where(GradeTrabalho.clinica_id == atual.clinica_id)
        .order_by(Dentista.nome, GradeTrabalho.dia_semana, GradeTrabalho.inicio)
    )
    return [
        GradeResumo(
            id=grade.id,
            dentista_id=grade.dentista_id,
            dentista=dentista,
            consultorio_id=grade.consultorio_id,
            consultorio=consultorio,
            dia_semana=grade.dia_semana,
            inicio=grade.inicio,
            fim=grade.fim,
        )
        for grade, dentista, consultorio in linhas.all()
    ]


async def _conferir_grade(sessao: AsyncSession, entrada: GradeEntrada, atual: UsuarioAtual) -> None:
    await _conferir_referencias(sessao, Dentista, [entrada.dentista_id], atual)
    await _conferir_referencias(sessao, Consultorio, [entrada.consultorio_id], atual)


@router.post("/grade", status_code=HTTPStatus.CREATED)
async def criar_grade(entrada: GradeEntrada, sessao: Sessao, atual: Admin) -> GradeResumo:
    await _conferir_grade(sessao, entrada, atual)
    grade = await _criar(sessao, atual, GradeTrabalho, "grade", entrada.model_dump())
    resposta = await _grade_resumo(sessao, grade)
    await sessao.commit()
    return resposta


@router.put("/grade/{id_}")
async def atualizar_grade(
    id_: UUID, entrada: GradeEntrada, sessao: Sessao, atual: Admin
) -> GradeResumo:
    await _conferir_grade(sessao, entrada, atual)
    grade = await _atualizar(sessao, atual, GradeTrabalho, "grade", id_, entrada.model_dump())
    resposta = await _grade_resumo(sessao, grade)
    await sessao.commit()
    return resposta


@router.delete("/grade/{id_}", status_code=HTTPStatus.NO_CONTENT)
async def apagar_grade(id_: UUID, sessao: Sessao, atual: Admin) -> None:
    await _apagar(sessao, atual, GradeTrabalho, "grade", id_)


def _bloqueio_resumo(bloqueio: Bloqueio, dentista: str | None) -> BloqueioResumo:
    assert bloqueio.periodo.lower is not None
    assert bloqueio.periodo.upper is not None
    return BloqueioResumo(
        id=bloqueio.id,
        dentista_id=bloqueio.dentista_id,
        dentista=dentista,
        inicio=bloqueio.periodo.lower,
        fim=bloqueio.periodo.upper,
        motivo=bloqueio.motivo,
    )


def _dados_do_bloqueio(entrada: BloqueioEntrada) -> dict[str, Any]:
    return {
        "dentista_id": entrada.dentista_id,
        "periodo": Range(entrada.inicio, entrada.fim),
        "motivo": entrada.motivo,
    }


@router.get("/bloqueios")
async def listar_bloqueios(sessao: Sessao, atual: Admin) -> list[BloqueioResumo]:
    # Só os que ainda valem: o seed cria um almoço por dia útil, e a lista cresceria para sempre.
    linhas = await sessao.execute(
        select(Bloqueio, Dentista.nome)
        .outerjoin(Dentista, Bloqueio.dentista_id == Dentista.id)
        .where(Bloqueio.clinica_id == atual.clinica_id, func.upper(Bloqueio.periodo) > func.now())
        .order_by(func.lower(Bloqueio.periodo))
    )
    return [_bloqueio_resumo(bloqueio, dentista) for bloqueio, dentista in linhas.all()]


async def _resumo_gravado(
    sessao: AsyncSession, bloqueio: Bloqueio, entrada: BloqueioEntrada
) -> BloqueioResumo:
    nome = None
    if entrada.dentista_id is not None:
        nome = (await sessao.get_one(Dentista, entrada.dentista_id)).nome
    return _bloqueio_resumo(bloqueio, nome)


@router.post("/bloqueios", status_code=HTTPStatus.CREATED)
async def criar_bloqueio(entrada: BloqueioEntrada, sessao: Sessao, atual: Admin) -> BloqueioResumo:
    if entrada.dentista_id is not None:
        await _conferir_referencias(sessao, Dentista, [entrada.dentista_id], atual)
    bloqueio = await _criar(sessao, atual, Bloqueio, "bloqueio", _dados_do_bloqueio(entrada))
    resposta = await _resumo_gravado(sessao, bloqueio, entrada)
    await sessao.commit()
    return resposta


@router.put("/bloqueios/{id_}")
async def atualizar_bloqueio(
    id_: UUID, entrada: BloqueioEntrada, sessao: Sessao, atual: Admin
) -> BloqueioResumo:
    if entrada.dentista_id is not None:
        await _conferir_referencias(sessao, Dentista, [entrada.dentista_id], atual)
    bloqueio = await _atualizar(
        sessao, atual, Bloqueio, "bloqueio", id_, _dados_do_bloqueio(entrada)
    )
    resposta = await _resumo_gravado(sessao, bloqueio, entrada)
    await sessao.commit()
    return resposta


@router.delete("/bloqueios/{id_}", status_code=HTTPStatus.NO_CONTENT)
async def apagar_bloqueio(id_: UUID, sessao: Sessao, atual: Admin) -> None:
    await _apagar(sessao, atual, Bloqueio, "bloqueio", id_)
