"""Base de conhecimento: o arquivo vai para o volume, e o worker recebe o pedido de indexar."""

import asyncio
import logging
from http import HTTPStatus
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from clinica_core.config import ConfigRabbit
from clinica_core.mensageria import (
    FILA_CONHECIMENTO_INDEXAR,
    LIMITE_PUBLICACAO_S,
    IndexarDocumento,
    abrir_canal,
    conectar,
    declarar_topologia,
    publicar,
)
from clinica_painel.api import ErroDaApi, chamar
from clinica_painel.telas import pagina

logger = logging.getLogger(__name__)

router = APIRouter()

EXTENSOES = {".pdf", ".docx", ".md", ".txt"}
TAMANHO_MAXIMO = 20 * 1024 * 1024
_BLOCO = 1024 * 1024
# Os textos escritos no painel são os únicos que dá para editar nele.
_PREFIXO_DE_TEXTO = "texto-"


class PublicadorDeIndexacao(Protocol):
    async def indexar(self, documento_id: UUID) -> None: ...


class PublicadorRabbit:
    def __init__(self, config: ConfigRabbit) -> None:
        self._config = config

    async def indexar(self, documento_id: UUID) -> None:
        async with asyncio.timeout(LIMITE_PUBLICACAO_S):
            conexao = await conectar(self._config)
            try:
                canal = await abrir_canal(conexao)
                await declarar_topologia(canal)
                await publicar(
                    canal, FILA_CONHECIMENTO_INDEXAR, IndexarDocumento(documento_id=documento_id)
                )
            finally:
                await conexao.close()


def caminho_no_volume(diretorio: Path, arquivo: str) -> Path:
    raiz = diretorio.resolve()
    caminho = (raiz / arquivo).resolve()
    if not caminho.is_relative_to(raiz):
        raise ErroDaApi(HTTPStatus.BAD_REQUEST, "arquivo_invalido")
    return caminho


def _caminho(request: Request, arquivo: str) -> Path:
    return caminho_no_volume(request.app.state.config.conhecimento_dir, arquivo)


async def _pedir_indexacao(request: Request, documento_id: str) -> str | None:
    publicador: PublicadorDeIndexacao = request.app.state.indexacao
    try:
        await publicador.indexar(UUID(documento_id))
    except Exception as erro:
        logger.warning("Indexação de %s não pedida: %r", documento_id, erro)
        # Sem isso, o documento ficaria em "processando" para sempre.
        await chamar(
            request, "PATCH", f"/painel/conhecimento/{documento_id}", json={"status": "erro"}
        )
        return "O arquivo foi salvo, mas a indexação não começou. Tente indexar de novo."
    return None


async def _tela(
    request: Request,
    status: int = HTTPStatus.OK,
    erro: str | None = None,
    pergunta: str = "",
    trechos: list[dict[str, Any]] | None = None,
    **extras: Any,
) -> Response:
    documentos = await chamar(request, "GET", "/painel/conhecimento")
    return pagina(
        request,
        "conhecimento.html",
        status,
        documentos=[
            {**d, "editavel": (d["arquivo"] or "").startswith(_PREFIXO_DE_TEXTO)}
            for d in documentos
        ],
        erro=erro,
        pergunta=pergunta,
        trechos=trechos,
        extensoes=",".join(sorted(EXTENSOES)),
        **extras,
    )


@router.get("/conhecimento")
async def ver(request: Request) -> Response:
    return await _tela(request)


async def _gravar_upload(arquivo: UploadFile, destino: Path) -> None:
    total = 0
    try:
        with destino.open("wb") as saida:  # noqa: ASYNC230
            while bloco := await arquivo.read(_BLOCO):
                total += len(bloco)
                if total > TAMANHO_MAXIMO:
                    raise ValueError("arquivo grande demais")
                saida.write(bloco)
    except Exception:
        destino.unlink(missing_ok=True)  # noqa: ASYNC240
        raise


async def _registrar(request: Request, titulo: str, nome_no_volume: str) -> Response:
    documento = await chamar(
        request,
        "POST",
        "/painel/conhecimento",
        json={"titulo": titulo, "arquivo": nome_no_volume},
    )
    aviso = await _pedir_indexacao(request, documento["id"])
    if aviso:
        return await _tela(request, HTTPStatus.ACCEPTED, aviso)
    return RedirectResponse("/conhecimento", HTTPStatus.SEE_OTHER)


@router.post("/conhecimento/enviar")
async def enviar(request: Request, arquivo: UploadFile, titulo: str = Form("")) -> Response:
    # Permissão primeiro: a recepção não chega a gravar nada no volume.
    await chamar(request, "GET", "/painel/conhecimento")
    extensao = Path(arquivo.filename or "").suffix.lower()
    if extensao not in EXTENSOES:
        return await _tela(
            request,
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Formato não aceito. Envie PDF, DOCX, MD ou TXT.",
        )
    # O nome vem do servidor: o do navegador nunca escolhe caminho no volume.
    nome = f"{uuid4().hex}{extensao}"
    destino = _caminho(request, nome)
    destino.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _gravar_upload(arquivo, destino)
    except ValueError:
        return await _tela(
            request, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "O arquivo passa de 20 MB."
        )
    titulo_final = titulo.strip() or Path(arquivo.filename or nome).stem
    try:
        return await _registrar(request, titulo_final, nome)
    except ErroDaApi:
        destino.unlink(missing_ok=True)
        raise


@router.post("/conhecimento/texto")
async def criar_texto(request: Request, titulo: str = Form(), texto: str = Form()) -> Response:
    await chamar(request, "GET", "/painel/conhecimento")
    if not texto.strip() or not titulo.strip():
        return await _tela(request, HTTPStatus.UNPROCESSABLE_ENTITY, "Preencha o título e o texto.")
    nome = f"{_PREFIXO_DE_TEXTO}{uuid4().hex}.md"
    destino = _caminho(request, nome)
    destino.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(destino.write_text, texto, "utf-8")
    try:
        return await _registrar(request, titulo.strip(), nome)
    except ErroDaApi:
        destino.unlink(missing_ok=True)
        raise


async def _documento_de_texto(request: Request, id_: str) -> dict[str, Any]:
    documento: dict[str, Any] = await chamar(request, "GET", f"/painel/conhecimento/{id_}")
    if not (documento["arquivo"] or "").startswith(_PREFIXO_DE_TEXTO):
        raise ErroDaApi(HTTPStatus.CONFLICT, "documento_nao_editavel")
    return documento


@router.get("/conhecimento/{id_}/editar")
async def ver_edicao(request: Request, id_: str) -> Response:
    documento = await _documento_de_texto(request, id_)
    caminho = _caminho(request, documento["arquivo"])
    texto = await asyncio.to_thread(caminho.read_text, "utf-8")
    return await _tela(request, edicao=documento, texto_da_edicao=texto)


@router.post("/conhecimento/{id_}/editar")
async def salvar_edicao(
    request: Request, id_: str, titulo: str = Form(), texto: str = Form()
) -> Response:
    documento = await _documento_de_texto(request, id_)
    if not texto.strip() or not titulo.strip():
        return await _tela(request, HTTPStatus.UNPROCESSABLE_ENTITY, "Preencha o título e o texto.")
    caminho = _caminho(request, documento["arquivo"])
    await asyncio.to_thread(caminho.write_text, texto, "utf-8")
    await chamar(
        request,
        "PATCH",
        f"/painel/conhecimento/{id_}",
        json={"titulo": titulo.strip(), "status": "processando"},
    )
    aviso = await _pedir_indexacao(request, id_)
    if aviso:
        return await _tela(request, HTTPStatus.ACCEPTED, aviso)
    return RedirectResponse("/conhecimento", HTTPStatus.SEE_OTHER)


@router.post("/conhecimento/{id_}/indexar")
async def indexar_de_novo(request: Request, id_: str) -> Response:
    await chamar(request, "PATCH", f"/painel/conhecimento/{id_}", json={"status": "processando"})
    aviso = await _pedir_indexacao(request, id_)
    if aviso:
        return await _tela(request, HTTPStatus.ACCEPTED, aviso)
    return RedirectResponse("/conhecimento", HTTPStatus.SEE_OTHER)


@router.post("/conhecimento/{id_}/ativo")
async def mudar_ativo(request: Request, id_: str, ativo: str = Form()) -> Response:
    await chamar(request, "PATCH", f"/painel/conhecimento/{id_}", json={"ativo": ativo == "1"})
    return RedirectResponse("/conhecimento", HTTPStatus.SEE_OTHER)


@router.post("/conhecimento/{id_}/apagar")
async def apagar(request: Request, id_: str) -> Response:
    documento = await chamar(request, "GET", f"/painel/conhecimento/{id_}")
    await chamar(request, "DELETE", f"/painel/conhecimento/{id_}")
    if documento["arquivo"]:
        _caminho(request, documento["arquivo"]).unlink(missing_ok=True)
    return RedirectResponse("/conhecimento", HTTPStatus.SEE_OTHER)


@router.post("/conhecimento/testar")
async def testar(request: Request, pergunta: str = Form()) -> Response:
    await chamar(request, "GET", "/painel/conhecimento")
    busca: httpx.AsyncClient = request.app.state.busca
    try:
        resposta = await busca.post(
            "/testar-pergunta",
            json={"clinica_id": request.session["clinica_id"], "pergunta": pergunta},
        )
        resposta.raise_for_status()
    except httpx.HTTPError as erro:
        logger.warning("Busca de teste indisponível: %r", erro)
        return await _tela(
            request,
            HTTPStatus.BAD_GATEWAY,
            "A busca está indisponível agora. Tente de novo em instantes.",
            pergunta=pergunta,
        )
    return await _tela(request, pergunta=pergunta, trechos=resposta.json()["trechos"])
