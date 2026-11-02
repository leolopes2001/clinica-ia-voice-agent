import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Text, cast, func, select
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_core.embeddings import GeradorEmbeddings
from clinica_core.modelos import ConhecimentoDocumento, ConhecimentoTrecho

logger = logging.getLogger(__name__)

CANDIDATOS = 20
MAXIMO_TRECHOS = 3
MAXIMO_CARACTERES = 800
_K_RRF = 60
_PALAVRA = re.compile(r"\w+")
_FRONTEIRA = re.compile(r"\n|(?<=[.!?])\s+")


@dataclass(frozen=True)
class Trecho:
    texto: str
    fonte: str


def combinar_rrf(*rankings: Sequence[UUID]) -> list[UUID]:
    """Reciprocal Rank Fusion. Um empate fica com quem apareceu primeiro."""
    pontos: dict[UUID, float] = {}
    for ranking in rankings:
        for posicao, id_ in enumerate(ranking, start=1):
            pontos[id_] = pontos.get(id_, 0.0) + 1 / (_K_RRF + posicao)
    return sorted(pontos, key=lambda id_: pontos[id_], reverse=True)


def recortar(texto: str, pergunta: str, limite: int = MAXIMO_CARACTERES) -> str:
    """Reduz o trecho ao limite, em torno das frases com mais palavras da pergunta."""
    if len(texto) <= limite:
        return texto
    primeira, separador, corpo = texto.partition("\n")
    # A primeira linha é o caminho de títulos quando o documento tem títulos.
    titulo = primeira + "\n" if separador and len(primeira) < limite / 4 else ""
    corpo = corpo if titulo else texto
    frases = [f.strip() for f in _FRONTEIRA.split(corpo) if f.strip()]
    procuradas = {p for p in _PALAVRA.findall(pergunta.lower()) if len(p) > 2}
    pontos = [len(procuradas & set(_PALAVRA.findall(f.lower()))) for f in frases]
    espaco = limite - len(titulo)

    melhor = max(range(len(frases)), key=lambda i: pontos[i], default=0)
    inicio = fim = melhor
    tamanho = len(frases[melhor]) if frases else 0
    if tamanho > espaco:
        return (titulo + frases[melhor][:espaco].rsplit(" ", 1)[0]).strip()
    # Expande para os lados enquanto couber, com preferência pela frase que mais pontua.
    while True:
        opcoes = []
        if inicio > 0 and tamanho + len(frases[inicio - 1]) + 1 <= espaco:
            opcoes.append((pontos[inicio - 1], 0, inicio - 1))
        if fim < len(frases) - 1 and tamanho + len(frases[fim + 1]) + 1 <= espaco:
            opcoes.append((pontos[fim + 1], 1, fim + 1))
        if not opcoes:
            break
        _, _, proxima = max(opcoes)
        tamanho += len(frases[proxima]) + 1
        inicio, fim = min(inicio, proxima), max(fim, proxima)
    return titulo + "\n".join(frases[inicio : fim + 1])


async def _por_vetor(sessao: AsyncSession, clinica_id: UUID, vetor: list[float]) -> list[UUID]:
    resultado = await sessao.scalars(
        select(ConhecimentoTrecho.id)
        .join(ConhecimentoDocumento)
        .where(ConhecimentoTrecho.clinica_id == clinica_id, ConhecimentoDocumento.ativo)
        .order_by(
            ConhecimentoTrecho.embedding.cosine_distance(vetor),
            ConhecimentoTrecho.id,
        )
        .limit(CANDIDATOS)
    )
    return list(resultado)


async def _por_texto(sessao: AsyncSession, clinica_id: UUID, pergunta: str) -> list[UUID]:
    # plainto_tsquery exige todas as palavras. Uma pergunta falada casa com qualquer uma delas.
    consulta = cast(
        func.replace(cast(func.plainto_tsquery("portuguese", pergunta), Text), "&", "|"),
        TSQUERY,
    )
    resultado = await sessao.scalars(
        select(ConhecimentoTrecho.id)
        .join(ConhecimentoDocumento)
        .where(
            ConhecimentoTrecho.clinica_id == clinica_id,
            ConhecimentoDocumento.ativo,
            ConhecimentoTrecho.tsv.op("@@")(consulta),
        )
        .order_by(func.ts_rank_cd(ConhecimentoTrecho.tsv, consulta).desc(), ConhecimentoTrecho.id)
        .limit(CANDIDATOS)
    )
    return list(resultado)


async def buscar(
    sessao: AsyncSession,
    gerador: GeradorEmbeddings,
    clinica_id: UUID,
    pergunta: str,
    k: int = MAXIMO_TRECHOS,
) -> list[Trecho]:
    k = max(1, min(k, MAXIMO_TRECHOS))
    try:
        vetor = await gerador.pergunta(pergunta)
    except Exception as erro:
        # Sem o Gemini, a busca por texto ainda responde boa parte das perguntas.
        logger.warning("Embedding da pergunta falhou; busca só por texto: %r", erro)
        ranking_vetor: list[UUID] = []
    else:
        ranking_vetor = await _por_vetor(sessao, clinica_id, vetor)
    ranking_texto = await _por_texto(sessao, clinica_id, pergunta)

    ids = combinar_rrf(ranking_vetor, ranking_texto)[:k]
    if not ids:
        return []
    linhas = await sessao.execute(
        select(ConhecimentoTrecho.id, ConhecimentoTrecho.texto, ConhecimentoDocumento.titulo)
        .join(ConhecimentoDocumento)
        .where(ConhecimentoTrecho.id.in_(ids))
    )
    por_id = {id_: (texto, titulo) for id_, texto, titulo in linhas.all()}
    return [Trecho(texto=recortar(por_id[id_][0], pergunta), fonte=por_id[id_][1]) for id_ in ids]
