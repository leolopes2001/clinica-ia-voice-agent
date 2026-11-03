"""Base de conhecimento, chamadas, pedidos de retorno e configurações, vistos pelo painel."""

from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_api import auditoria
from clinica_api.agenda import ErroAgenda
from clinica_api.autenticacao import Admin, Sessao, UsuarioAtual
from clinica_api.esquemas import _Entrada
from clinica_core.modelos import ConhecimentoDocumento

router = APIRouter(prefix="/painel")

_Texto = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class DocumentoEntrada(_Entrada):
    titulo: _Texto
    arquivo: _Texto


class DocumentoAtualizar(_Entrada):
    titulo: _Texto | None = None
    ativo: bool | None = None
    # Pede a indexação de novo, ou registra que o pedido não chegou ao worker.
    status: str | None = None


class DocumentoResumo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    titulo: str
    arquivo: str | None
    status: str
    ativo: bool


async def _documento(sessao: AsyncSession, id_: UUID, atual: UsuarioAtual) -> ConhecimentoDocumento:
    documento = await sessao.scalar(
        select(ConhecimentoDocumento).where(
            ConhecimentoDocumento.id == id_,
            ConhecimentoDocumento.clinica_id == atual.clinica_id,
        )
    )
    if documento is None:
        raise ErroAgenda("registro_nao_encontrado")
    return documento


@router.get("/conhecimento")
async def listar_documentos(sessao: Sessao, atual: Admin) -> list[DocumentoResumo]:
    documentos = await sessao.scalars(
        select(ConhecimentoDocumento)
        .where(ConhecimentoDocumento.clinica_id == atual.clinica_id)
        .order_by(ConhecimentoDocumento.titulo)
    )
    return [DocumentoResumo.model_validate(d) for d in documentos]


@router.get("/conhecimento/{id_}")
async def ver_documento(id_: UUID, sessao: Sessao, atual: Admin) -> DocumentoResumo:
    return DocumentoResumo.model_validate(await _documento(sessao, id_, atual))


@router.post("/conhecimento", status_code=HTTPStatus.CREATED)
async def criar_documento(
    entrada: DocumentoEntrada, sessao: Sessao, atual: Admin
) -> DocumentoResumo:
    documento = ConhecimentoDocumento(
        clinica_id=atual.clinica_id, titulo=entrada.titulo, arquivo=entrada.arquivo
    )
    sessao.add(documento)
    await sessao.flush()
    await sessao.refresh(documento)
    resposta = DocumentoResumo.model_validate(documento)
    auditoria.registrar(
        sessao,
        atual.clinica_id,
        atual.id,
        "criar",
        "conhecimento",
        depois={"id": str(documento.id), "titulo": entrada.titulo},
    )
    await sessao.commit()
    return resposta


@router.patch("/conhecimento/{id_}")
async def atualizar_documento(
    id_: UUID, entrada: DocumentoAtualizar, sessao: Sessao, atual: Admin
) -> DocumentoResumo:
    documento = await _documento(sessao, id_, atual)
    if entrada.status is not None:
        if entrada.status not in {"processando", "erro"}:
            raise ErroAgenda("status_invalido")
        documento.status = entrada.status
    if entrada.titulo is not None:
        documento.titulo = entrada.titulo
    if entrada.ativo is not None:
        documento.ativo = entrada.ativo
    resposta = DocumentoResumo.model_validate(documento)
    auditoria.registrar(
        sessao,
        atual.clinica_id,
        atual.id,
        "atualizar",
        "conhecimento",
        depois={"id": str(id_), **entrada.model_dump(exclude_none=True)},
    )
    await sessao.commit()
    return resposta


@router.delete("/conhecimento/{id_}", status_code=HTTPStatus.NO_CONTENT)
async def apagar_documento(id_: UUID, sessao: Sessao, atual: Admin) -> None:
    # Os trechos vão junto, pela FK com CASCADE. O arquivo fica a cargo do painel.
    documento = await _documento(sessao, id_, atual)
    titulo = documento.titulo
    await sessao.delete(documento)
    auditoria.registrar(
        sessao,
        atual.clinica_id,
        atual.id,
        "apagar",
        "conhecimento",
        antes={"id": str(id_), "titulo": titulo},
    )
    await sessao.commit()
