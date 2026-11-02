import asyncio
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import docx
from aio_pika.abc import AbstractIncomingMessage
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clinica_core.embeddings import GeradorEmbeddings
from clinica_core.mensageria import IndexarDocumento
from clinica_core.modelos import ConhecimentoDocumento, ConhecimentoTrecho

logger = logging.getLogger(__name__)

# Cerca de 4 caracteres por token em português: 500 tokens, com 15% de sobreposição.
TAMANHO_TRECHO = 2000
SOBREPOSICAO = 300

_TITULO = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_SEPARADOR_TABELA = re.compile(r"^\|?[\s:|-]+\|?$")
_FIM_DE_FRASE = re.compile(r"(?<=[.!?])\s+")


def _extrair_pdf(caminho: Path) -> str:
    paginas = (pagina.extract_text() for pagina in PdfReader(caminho).pages)
    return "\n\n".join(pagina for pagina in paginas if pagina)


def _extrair_docx(caminho: Path) -> str:
    linhas: list[str] = []
    for bloco in docx.Document(str(caminho)).iter_inner_content():
        if isinstance(bloco, Paragraph):
            estilo = bloco.style.name if bloco.style is not None else ""
            nivel = re.fullmatch(r"(?:Heading|Título|Title)\s*(\d?)", estilo or "")
            prefixo = "#" * int(nivel.group(1) or 1) + " " if nivel and bloco.text else ""
            linhas.append(prefixo + bloco.text)
        elif isinstance(bloco, Table):
            for fileira in bloco.rows:
                linhas.append(" | ".join(celula.text.strip() for celula in fileira.cells))
    return "\n".join(linhas)


def extrair_texto(caminho: Path) -> str:
    sufixo = caminho.suffix.lower()
    if sufixo == ".pdf":
        return _extrair_pdf(caminho)
    if sufixo == ".docx":
        return _extrair_docx(caminho)
    if sufixo in {".md", ".txt"}:
        return caminho.read_text(encoding="utf-8")
    raise ValueError(f"Formato não suportado: {sufixo}")


def _secoes(texto: str) -> Iterator[tuple[str, list[str]]]:
    """Separa o texto pelos títulos e devolve o caminho de títulos e as linhas de cada seção."""
    titulos: list[tuple[int, str]] = []
    linhas: list[str] = []

    def caminho() -> str:
        return " > ".join(titulo for _, titulo in titulos)

    for linha in texto.splitlines():
        achado = _TITULO.match(linha.strip())
        if achado is None:
            if linha.strip() and not _SEPARADOR_TABELA.match(linha.strip()):
                linhas.append(linha.strip())
            continue
        if linhas:
            yield caminho(), linhas
            linhas = []
        nivel = len(achado.group(1))
        titulos = [t for t in titulos if t[0] < nivel] + [(nivel, achado.group(2))]
    if linhas:
        yield caminho(), linhas


def _unidades(linhas: list[str], tamanho: int) -> Iterator[str]:
    for linha in linhas:
        for frase in _FIM_DE_FRASE.split(linha):
            # Frase maior que o trecho: corta nas palavras.
            while len(frase) > tamanho:
                corte = frase.rfind(" ", 0, tamanho)
                corte = corte if corte > 0 else tamanho
                yield frase[:corte]
                frase = frase[corte:].strip()
            if frase:
                yield frase


def _fatiar_secao(linhas: list[str], tamanho: int, sobreposicao: int) -> Iterator[str]:
    atual: list[str] = []
    total = 0
    for unidade in _unidades(linhas, tamanho):
        if atual and total + len(unidade) + 1 > tamanho:
            yield "\n".join(atual)
            # Repete o fim do trecho anterior, sempre em frases inteiras.
            repetidas: list[str] = []
            repetido = 0
            for anterior in reversed(atual):
                if repetido + len(anterior) + 1 > sobreposicao:
                    break
                repetidas.insert(0, anterior)
                repetido += len(anterior) + 1
            # Só repete o que couber junto com a próxima frase.
            if repetido + len(unidade) + 1 > tamanho:
                repetidas, repetido = [], 0
            atual, total = repetidas, repetido
        atual.append(unidade)
        total += len(unidade) + 1
    if atual:
        yield "\n".join(atual)


def fatiar(
    texto: str, tamanho: int = TAMANHO_TRECHO, sobreposicao: int = SOBREPOSICAO
) -> list[str]:
    """Um título nunca divide um trecho, e cada trecho carrega o caminho dos títulos."""
    trechos: list[str] = []
    for caminho, linhas in _secoes(texto):
        prefixo = f"{caminho}\n" if caminho else ""
        espaco = max(tamanho - len(prefixo), sobreposicao + 1)
        trechos += [prefixo + corpo for corpo in _fatiar_secao(linhas, espaco, sobreposicao)]
    return trechos


def resolver_arquivo(diretorio: Path, arquivo: str) -> Path:
    raiz = diretorio.resolve()
    caminho = (raiz / arquivo).resolve()
    if not caminho.is_relative_to(raiz):
        raise ValueError("Arquivo fora do diretório da base de conhecimento")
    return caminho


async def indexar_documento(
    fabrica: async_sessionmaker[AsyncSession],
    gerador: GeradorEmbeddings,
    diretorio: Path,
    documento_id: UUID,
) -> None:
    async with fabrica() as sessao:
        documento = await sessao.get(ConhecimentoDocumento, documento_id)
    if documento is None:
        logger.warning("Documento %s não existe mais: nada a indexar", documento_id)
        return
    if documento.arquivo is None:
        raise ValueError("Documento sem arquivo")

    caminho = resolver_arquivo(diretorio, documento.arquivo)
    # O texto e os embeddings vêm antes da transação, para não prender o banco.
    texto = await asyncio.to_thread(extrair_texto, caminho)
    trechos = fatiar(texto)
    if not trechos:
        raise ValueError("Documento sem texto extraível")
    embeddings = await gerador.documentos(trechos)

    async with fabrica() as sessao:
        travado = await sessao.scalar(
            select(ConhecimentoDocumento)
            .where(ConhecimentoDocumento.id == documento_id)
            .with_for_update()
        )
        if travado is None:
            logger.warning("Documento %s apagado durante a indexação", documento_id)
            return
        # Na mesma transação: a busca nunca vê o documento pela metade.
        await sessao.execute(
            delete(ConhecimentoTrecho).where(ConhecimentoTrecho.documento_id == documento_id)
        )
        sessao.add_all(
            ConhecimentoTrecho(
                clinica_id=travado.clinica_id,
                documento_id=documento_id,
                ordem=ordem,
                texto=trecho,
                embedding=embedding,
            )
            for ordem, (trecho, embedding) in enumerate(zip(trechos, embeddings, strict=True))
        )
        travado.status = "pronto"
        await sessao.commit()


async def _marcar_erro(fabrica: async_sessionmaker[AsyncSession], documento_id: UUID) -> None:
    async with fabrica() as sessao:
        await sessao.execute(
            update(ConhecimentoDocumento)
            .where(ConhecimentoDocumento.id == documento_id)
            .values(status="erro")
        )
        await sessao.commit()


async def processar_mensagem(
    fabrica: async_sessionmaker[AsyncSession],
    gerador: GeradorEmbeddings,
    diretorio: Path,
    mensagem: AbstractIncomingMessage,
) -> None:
    documento_id: UUID | None = None
    try:
        documento_id = IndexarDocumento.model_validate_json(mensagem.body).documento_id
        await indexar_documento(fabrica, gerador, diretorio, documento_id)
    except Exception:
        logger.exception("Indexação de %s enviada para a DLQ", documento_id)
        if documento_id is not None:
            try:
                await _marcar_erro(fabrica, documento_id)
            except Exception:
                logger.exception("Não foi possível marcar o documento %s com erro", documento_id)
        await mensagem.reject(requeue=False)
    else:
        await mensagem.ack()
