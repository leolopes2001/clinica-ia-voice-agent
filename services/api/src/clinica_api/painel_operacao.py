"""Base de conhecimento, chamadas, pedidos de retorno e configurações, vistos pelo painel."""

from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_api import auditoria
from clinica_api.agenda import ErroAgenda
from clinica_api.autenticacao import Admin, Logado, Sessao, UsuarioAtual
from clinica_api.esquemas import _Entrada
from clinica_core.modelos import Chamada, ConhecimentoDocumento, PedidoRetorno

router = APIRouter(prefix="/painel")

MAXIMO_CHAMADAS = 200
_IDADE_MAXIMA_DE_CHAMADA_ATIVA = timedelta(hours=4)
MAXIMO_RETORNOS = 200
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


class ChamadaResumo(BaseModel):
    id: UUID
    # Vazio: número oculto, ou prazo de retenção já vencido.
    telefone: str | None
    inicio: datetime
    fim: datetime | None
    duracao_s: int | None
    desfecho: str | None
    consentiu: bool
    recusou: bool
    tem_transcricao: bool


class ChamadaCompleta(ChamadaResumo):
    transcricao: str | None
    apagar_em: datetime | None


def _resumo_da_chamada(chamada: Chamada) -> dict[str, object]:
    duracao = (chamada.fim - chamada.inicio).total_seconds() if chamada.fim else None
    return {
        "id": chamada.id,
        "telefone": chamada.telefone,
        "inicio": chamada.inicio,
        "fim": chamada.fim,
        "duracao_s": round(duracao) if duracao is not None else None,
        "desfecho": chamada.desfecho,
        "consentiu": chamada.consentimento_em is not None,
        "recusou": chamada.consentimento_recusado_em is not None,
        "tem_transcricao": chamada.transcricao is not None,
    }


@router.get("/chamadas")
async def listar_chamadas(
    sessao: Sessao, atual: Admin, desfecho: str | None = None
) -> list[ChamadaResumo]:
    consulta = select(Chamada).where(Chamada.clinica_id == atual.clinica_id)
    if desfecho:
        consulta = consulta.where(Chamada.desfecho == desfecho)
    chamadas = await sessao.scalars(consulta.order_by(Chamada.inicio.desc()).limit(MAXIMO_CHAMADAS))
    resposta = [ChamadaResumo.model_validate(_resumo_da_chamada(c)) for c in chamadas]
    auditoria.registrar(
        sessao,
        atual.clinica_id,
        atual.id,
        "ver",
        "chamada",
        depois={"quantidade": len(resposta)},
    )
    await sessao.commit()
    return resposta


@router.get("/chamadas/{id_}")
async def ver_chamada(id_: UUID, sessao: Sessao, atual: Admin) -> ChamadaCompleta:
    chamada = await sessao.scalar(
        select(Chamada).where(Chamada.id == id_, Chamada.clinica_id == atual.clinica_id)
    )
    if chamada is None:
        raise ErroAgenda("registro_nao_encontrado")
    resposta = ChamadaCompleta(
        **_resumo_da_chamada(chamada),  # type: ignore[arg-type]
        transcricao=chamada.transcricao,
        apagar_em=chamada.apagar_em,
    )
    # Quem leu qual transcrição: nunca o texto dela.
    auditoria.registrar(
        sessao,
        atual.clinica_id,
        atual.id,
        "ver_transcricao" if chamada.transcricao is not None else "ver",
        "chamada",
        depois={"id": str(id_)},
    )
    await sessao.commit()
    return resposta


class RetornoResumo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    protocolo: str
    telefone: str | None
    motivo: str | None
    status: str
    criado_em: datetime


@router.get("/retornos")
async def listar_retornos(sessao: Sessao, atual: Logado) -> list[RetornoResumo]:
    retornos = await sessao.scalars(
        select(PedidoRetorno)
        .where(PedidoRetorno.clinica_id == atual.clinica_id)
        # Os pendentes primeiro: é o que a recepção precisa resolver.
        .order_by(PedidoRetorno.status.desc(), PedidoRetorno.criado_em.desc())
        .limit(MAXIMO_RETORNOS)
    )
    resposta = [RetornoResumo.model_validate(r) for r in retornos]
    auditoria.registrar(
        sessao,
        atual.clinica_id,
        atual.id,
        "ver",
        "pedido_retorno",
        depois={"quantidade": len(resposta)},
    )
    await sessao.commit()
    return resposta


@router.post("/retornos/{id_}/concluir")
async def concluir_retorno(id_: UUID, sessao: Sessao, atual: Logado) -> RetornoResumo:
    pedido = await sessao.scalar(
        select(PedidoRetorno).where(
            PedidoRetorno.id == id_, PedidoRetorno.clinica_id == atual.clinica_id
        )
    )
    if pedido is None:
        raise ErroAgenda("registro_nao_encontrado")
    pedido.status = "concluido"
    resposta = RetornoResumo.model_validate(pedido)
    auditoria.registrar(
        sessao,
        atual.clinica_id,
        atual.id,
        "concluir",
        "pedido_retorno",
        depois={"id": str(id_)},
    )
    await sessao.commit()
    return resposta


@router.get("/saude")
async def saude(sessao: Sessao, atual: Admin) -> dict[str, object]:
    # Uma ligação não dura tanto: o que passou disso é de um agente que caiu antes de encerrar.
    ativas = await sessao.scalar(
        select(func.count())
        .select_from(Chamada)
        .where(
            Chamada.clinica_id == atual.clinica_id,
            Chamada.fim.is_(None),
            Chamada.inicio > func.now() - _IDADE_MAXIMA_DE_CHAMADA_ATIVA,
        )
    )
    return {"banco": "ok", "chamadas_ativas": ativas}
