import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from uuid import UUID, uuid4

import docx
import pytest
from aio_pika.abc import AbstractChannel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from clinica_core.config import ConfigConhecimento, ConfigPostgres, ConfigRabbit
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
    DIMENSAO_EMBEDDING,
    Clinica,
    ConhecimentoDocumento,
    ConhecimentoTrecho,
)
from clinica_worker.conhecimento import (
    SOBREPOSICAO,
    TAMANHO_TRECHO,
    extrair_texto,
    fatiar,
    indexar_documento,
    processar_mensagem,
    resolver_arquivo,
)
from clinica_worker.principal import rodar

Fabrica = async_sessionmaker[AsyncSession]


class GeradorFalso:
    def __init__(self, erro: Exception | None = None) -> None:
        self.erro = erro
        self.tamanhos: list[int] = []

    async def documentos(self, textos: Sequence[str]) -> list[list[float]]:
        if self.erro is not None:
            raise self.erro
        self.tamanhos.append(len(textos))
        return [[1.0] + [0.0] * (DIMENSAO_EMBEDDING - 1) for _ in textos]

    async def pergunta(self, texto: str) -> list[float]:
        return [1.0] + [0.0] * (DIMENSAO_EMBEDDING - 1)


class MensagemFalsa:
    def __init__(self, corpo: bytes) -> None:
        self.body = corpo
        self.acoes: list[str] = []

    async def ack(self) -> None:
        self.acoes.append("ack")

    async def reject(self, requeue: bool = False) -> None:
        self.acoes.append(f"reject:{requeue}")


def _frases(quantas: int, inicio: int = 0) -> str:
    return " ".join(f"Frase numero {i} com algum texto de exemplo." for i in range(inicio, quantas))


def _pdf_simples(texto: str) -> bytes:
    fluxo = f"BT /F1 12 Tf 50 700 Td ({texto}) Tj ET".encode()
    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(fluxo)).encode() + b" >>\nstream\n" + fluxo + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    corpo = b"%PDF-1.4\n"
    posicoes = []
    for numero, objeto in enumerate(objetos, start=1):
        posicoes.append(len(corpo))
        corpo += f"{numero} 0 obj\n".encode() + objeto + b"\nendobj\n"
    inicio_xref = len(corpo)
    corpo += f"xref\n0 {len(objetos) + 1}\n0000000000 65535 f \n".encode()
    corpo += b"".join(f"{posicao:010d} 00000 n \n".encode() for posicao in posicoes)
    corpo += f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\n".encode()
    return corpo + f"startxref\n{inicio_xref}\n%%EOF\n".encode()


# ---- Extração


@pytest.mark.parametrize("sufixo", [".md", ".txt", ".MD"])
def test_extrai_texto_puro(tmp_path: Path, sufixo: str) -> None:
    caminho = tmp_path / f"a{sufixo}"
    caminho.write_text("Avaliação gratuita", encoding="utf-8")
    assert extrair_texto(caminho) == "Avaliação gratuita"


def test_extrai_pdf(tmp_path: Path) -> None:
    caminho = tmp_path / "a.pdf"
    caminho.write_bytes(_pdf_simples("Limpeza custa 150 reais"))
    assert "Limpeza custa 150 reais" in extrair_texto(caminho)


def test_extrai_docx_com_titulos_e_tabela(tmp_path: Path) -> None:
    documento = docx.Document()
    documento.add_heading("Preços", level=1)
    documento.add_paragraph("Valores de referência.")
    tabela = documento.add_table(rows=1, cols=2)
    tabela.rows[0].cells[0].text = "Limpeza"
    tabela.rows[0].cells[1].text = "R$ 150"
    caminho = tmp_path / "a.docx"
    documento.save(str(caminho))
    assert extrair_texto(caminho).splitlines() == [
        "# Preços",
        "Valores de referência.",
        "Limpeza | R$ 150",
    ]


def test_formato_nao_suportado(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="não suportado"):
        extrair_texto(tmp_path / "a.xlsx")


# ---- Fatiamento


def test_titulo_nunca_fica_dentro_do_trecho_de_outra_secao() -> None:
    trechos = fatiar("# Preços\n## Limpeza\nCusta 150.\n## Canal\nCusta 900.\n# Convênios\nAmil.")
    assert trechos == [
        "Preços > Limpeza\nCusta 150.",
        "Preços > Canal\nCusta 900.",
        "Convênios\nAmil.",
    ]


def test_subir_de_nivel_volta_no_caminho_de_titulos() -> None:
    trechos = fatiar("# A\n## B\n### C\nx\n## D\ny")
    assert trechos == ["A > B > C\nx", "A > D\ny"]


def test_secao_grande_vira_trechos_de_cerca_de_500_tokens_com_sobreposicao() -> None:
    trechos = fatiar("# Título\n" + _frases(300))
    assert len(trechos) > 2
    for trecho in trechos:
        assert len(trecho) <= TAMANHO_TRECHO
        assert trecho.startswith("Título\n")
    for anterior, seguinte in zip(trechos, trechos[1:], strict=False):
        fim_anterior = anterior.splitlines()[-1]
        assert fim_anterior in seguinte
        repetido = anterior[anterior.index(seguinte.splitlines()[1]) :]
        # 15% de 2000 caracteres, em frases inteiras.
        assert 0 < len(repetido) <= SOBREPOSICAO


def test_nenhuma_frase_se_perde() -> None:
    trechos = "\n".join(fatiar(_frases(300)))
    for i in range(300):
        assert f"Frase numero {i} com" in trechos


def test_frase_gigante_e_cortada_nas_palavras() -> None:
    trechos = fatiar("palavra " * 1000)
    assert len(trechos) > 1
    assert all(len(t) <= TAMANHO_TRECHO for t in trechos)
    assert all(set(t.split()) == {"palavra"} for t in trechos)


def test_texto_sem_titulo() -> None:
    assert fatiar("Só um parágrafo.") == ["Só um parágrafo."]


def test_tabela_markdown_descarta_a_linha_separadora() -> None:
    assert fatiar("# P\n| A | B |\n|---|:--|\n| c | d |") == ["P\n| A | B |\n| c | d |"]


def test_titulo_sem_conteudo_e_texto_vazio_nao_geram_trecho() -> None:
    assert fatiar("# A\n\n# B\n") == []
    assert fatiar("   \n") == []


# ---- Caminho do arquivo


def test_caminho_dentro_do_diretorio(tmp_path: Path) -> None:
    assert resolver_arquivo(tmp_path, "sub/a.md") == (tmp_path / "sub" / "a.md").resolve()


@pytest.mark.parametrize("arquivo", ["../segredo.txt", "/etc/passwd", "sub/../../x"])
def test_caminho_fora_do_diretorio_e_recusado(tmp_path: Path, arquivo: str) -> None:
    with pytest.raises(ValueError, match="fora do diretório"):
        resolver_arquivo(tmp_path, arquivo)


# ---- Indexação no banco


@pytest.fixture
async def engine(banco: str) -> AsyncIterator[AsyncEngine]:
    engine = criar_engine(ConfigPostgres(db=banco))
    yield engine
    await engine.dispose()


@pytest.fixture
def fabrica(engine: AsyncEngine) -> Fabrica:
    return criar_fabrica_sessoes(engine)


async def _documento(fabrica: Fabrica, arquivo: str | None) -> UUID:
    async with fabrica() as sessao:
        clinica = Clinica(nome="C", telefone="+5511", endereco="R", politica_cancelamento_horas=24)
        sessao.add(clinica)
        await sessao.flush()
        documento = ConhecimentoDocumento(clinica_id=clinica.id, titulo="Preços", arquivo=arquivo)
        sessao.add(documento)
        await sessao.commit()
        return documento.id


async def _trechos(fabrica: Fabrica, documento_id: UUID) -> list[ConhecimentoTrecho]:
    async with fabrica() as sessao:
        return list(
            await sessao.scalars(
                select(ConhecimentoTrecho)
                .where(ConhecimentoTrecho.documento_id == documento_id)
                .order_by(ConhecimentoTrecho.ordem)
            )
        )


async def _status(fabrica: Fabrica, documento_id: UUID) -> str:
    async with fabrica() as sessao:
        return (await sessao.get_one(ConhecimentoDocumento, documento_id)).status


async def test_indexar_grava_trechos_com_vetor_e_tsvector(fabrica: Fabrica, tmp_path: Path) -> None:
    (tmp_path / "p.md").write_text("# Preços\n## Limpeza\nCusta 150.\n## Canal\nCusta 900.")
    documento_id = await _documento(fabrica, "p.md")
    await indexar_documento(fabrica, GeradorFalso(), tmp_path, documento_id)

    trechos = await _trechos(fabrica, documento_id)
    assert [t.ordem for t in trechos] == [0, 1]
    assert trechos[0].texto == "Preços > Limpeza\nCusta 150."
    assert len(trechos[0].embedding) == DIMENSAO_EMBEDDING
    assert "limpez" in trechos[0].tsv
    assert await _status(fabrica, documento_id) == "pronto"


async def test_reindexar_substitui_os_trechos(fabrica: Fabrica, tmp_path: Path) -> None:
    arquivo = tmp_path / "p.md"
    arquivo.write_text("# A\nprimeiro\n# B\nsegundo")
    documento_id = await _documento(fabrica, "p.md")
    await indexar_documento(fabrica, GeradorFalso(), tmp_path, documento_id)
    arquivo.write_text("# C\nnovo")
    await indexar_documento(fabrica, GeradorFalso(), tmp_path, documento_id)
    assert [t.texto for t in await _trechos(fabrica, documento_id)] == ["C\nnovo"]


async def test_mensagem_ok_confirma(fabrica: Fabrica, tmp_path: Path) -> None:
    (tmp_path / "p.md").write_text("texto")
    documento_id = await _documento(fabrica, "p.md")
    mensagem = MensagemFalsa(IndexarDocumento(documento_id=documento_id).model_dump_json().encode())
    await processar_mensagem(fabrica, GeradorFalso(), tmp_path, mensagem)  # type: ignore[arg-type]
    assert mensagem.acoes == ["ack"]


async def test_falha_nos_embeddings_marca_erro_e_vai_para_a_dlq(
    fabrica: Fabrica, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "p.md").write_text("texto")
    documento_id = await _documento(fabrica, "p.md")
    await indexar_documento(fabrica, GeradorFalso(), tmp_path, documento_id)

    mensagem = MensagemFalsa(IndexarDocumento(documento_id=documento_id).model_dump_json().encode())
    gerador = GeradorFalso(RuntimeError("fora do ar"))
    with caplog.at_level(logging.ERROR):
        await processar_mensagem(fabrica, gerador, tmp_path, mensagem)  # type: ignore[arg-type]
    assert mensagem.acoes == ["reject:False"]
    assert await _status(fabrica, documento_id) == "erro"
    # A busca continua com a versão anterior até a próxima indexação dar certo.
    assert [t.texto for t in await _trechos(fabrica, documento_id)] == ["texto"]


@pytest.mark.parametrize(
    ("arquivo", "conteudo"),
    [("inexistente.md", None), ("vazio.md", "   "), (None, None), ("../fora.md", None)],
)
async def test_documento_invalido_marca_erro(
    fabrica: Fabrica, tmp_path: Path, arquivo: str | None, conteudo: str | None
) -> None:
    if arquivo is not None and conteudo is not None:
        (tmp_path / arquivo).write_text(conteudo)
    documento_id = await _documento(fabrica, arquivo)
    mensagem = MensagemFalsa(IndexarDocumento(documento_id=documento_id).model_dump_json().encode())
    await processar_mensagem(fabrica, GeradorFalso(), tmp_path, mensagem)  # type: ignore[arg-type]
    assert mensagem.acoes == ["reject:False"]
    assert await _status(fabrica, documento_id) == "erro"


async def test_mensagem_invalida_vai_para_a_dlq(fabrica: Fabrica, tmp_path: Path) -> None:
    mensagem = MensagemFalsa(b"nao e json")
    await processar_mensagem(fabrica, GeradorFalso(), tmp_path, mensagem)  # type: ignore[arg-type]
    assert mensagem.acoes == ["reject:False"]


async def test_documento_apagado_so_confirma(fabrica: Fabrica, tmp_path: Path) -> None:
    mensagem = MensagemFalsa(IndexarDocumento(documento_id=uuid4()).model_dump_json().encode())
    await processar_mensagem(fabrica, GeradorFalso(), tmp_path, mensagem)  # type: ignore[arg-type]
    assert mensagem.acoes == ["ack"]


async def _esperar_status(fabrica: Fabrica, documento_id: UUID, status: str) -> None:
    async with asyncio.timeout(10):
        while await _status(fabrica, documento_id) != status:  # noqa: ASYNC110
            await asyncio.sleep(0.05)


async def test_worker_consome_a_fila_de_indexacao(
    banco: str, fabrica: Fabrica, tmp_path: Path, vhost: str
) -> None:
    (tmp_path / "p.md").write_text("# Preços\nLimpeza custa 150.")
    documento_id = await _documento(fabrica, "p.md")
    parar = asyncio.Event()
    tarefa = asyncio.create_task(
        rodar(
            ConfigPostgres(db=banco),
            ConfigRabbit(),
            parar,
            arquivo_vivo=tmp_path / "vivo",
            gerador=GeradorFalso(),
            config_conhecimento=ConfigConhecimento(dir=tmp_path),
        )
    )
    conexao = await conectar(ConfigRabbit())
    try:
        canal: AbstractChannel = await abrir_canal(conexao)
        await declarar_topologia(canal)
        await publicar(
            canal, FILA_CONHECIMENTO_INDEXAR, IndexarDocumento(documento_id=documento_id)
        )
        await _esperar_status(fabrica, documento_id, "pronto")
    finally:
        await conexao.close()
        parar.set()
        await tarefa
    assert len(await _trechos(fabrica, documento_id)) == 1
