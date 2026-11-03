from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from clinica_core.modelos import Auditoria, ConhecimentoDocumento, ConhecimentoTrecho
from clinica_painel import conhecimento
from clinica_painel.api import ErroDaApi
from clinica_worker.conhecimento import indexar_documento


async def _documentos(ambiente: Any) -> list[ConhecimentoDocumento]:
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        return list(
            await sessao.scalars(
                select(ConhecimentoDocumento).where(
                    ConhecimentoDocumento.clinica_id == ambiente.clinica_id
                )
            )
        )


async def _enviar(
    cliente: httpx.AsyncClient, nome: str = "precos.md", conteudo: bytes = b"# Precos\nLimpeza 100"
) -> httpx.Response:
    return await cliente.post(
        "/conhecimento/enviar", files={"arquivo": (nome, conteudo)}, data={"titulo": "Preços"}
    )


class _GeradorFalso:
    async def documentos(self, textos: Any) -> list[list[float]]:
        return [[0.1] * 768 for _ in textos]

    async def pergunta(self, texto: str) -> list[float]:
        return [0.1] * 768


async def test_recepcao_nao_ve_a_base_nem_grava_no_volume(
    recepcao: httpx.AsyncClient, ambiente: Any
) -> None:
    assert (await recepcao.get("/conhecimento")).status_code == 403
    assert (await _enviar(recepcao)).status_code == 403
    assert list(ambiente.diretorio.iterdir()) == []
    assert ambiente.indexacao.pedidos == []


@pytest.mark.parametrize("nome", ["base.pdf", "base.docx", "base.md", "BASE.TXT"])
async def test_formatos_aceitos(admin: httpx.AsyncClient, ambiente: Any, nome: str) -> None:
    assert (await _enviar(admin, nome)).status_code == 303
    documento = (await _documentos(ambiente))[-1]
    assert documento.status == "processando"
    assert documento.id in ambiente.indexacao.pedidos


@pytest.mark.parametrize("nome", ["virus.exe", "sem-extensao", "base.html"])
async def test_formato_recusado_nao_grava(
    admin: httpx.AsyncClient, ambiente: Any, nome: str
) -> None:
    assert (await _enviar(admin, nome)).status_code == 422
    assert list(ambiente.diretorio.iterdir()) == []


async def test_nome_do_navegador_nao_escolhe_o_caminho(
    admin: httpx.AsyncClient, ambiente: Any
) -> None:
    assert (await _enviar(admin, "../../../etc/fora.md")).status_code == 303
    gravados = list(ambiente.diretorio.iterdir())
    assert len(gravados) == 1
    assert "fora" not in gravados[0].name
    assert not (ambiente.diretorio.parent / "fora.md").exists()


async def test_arquivo_grande_demais_nao_fica_no_volume(
    admin: httpx.AsyncClient, ambiente: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conhecimento, "TAMANHO_MAXIMO", 10)
    resposta = await _enviar(admin, "grande.md", b"x" * 11)
    assert resposta.status_code == 413
    assert "20 MB" in resposta.text
    assert list(ambiente.diretorio.iterdir()) == []
    assert await _documentos(ambiente) == []


async def test_broker_fora_do_ar_nao_deixa_processando_para_sempre(
    admin: httpx.AsyncClient, ambiente: Any
) -> None:
    ambiente.indexacao.erro = RuntimeError("sem broker")
    resposta = await _enviar(admin)
    assert resposta.status_code == 202
    assert "a indexação não começou" in resposta.text
    documento = (await _documentos(ambiente))[0]
    assert documento.status == "erro"
    ambiente.indexacao.erro = None
    assert (await admin.post(f"/conhecimento/{documento.id}/indexar")).status_code == 303
    assert (await _documentos(ambiente))[0].status == "processando"


async def test_tela_mostra_o_status_e_atualiza_sozinha(admin: httpx.AsyncClient) -> None:
    await _enviar(admin)
    texto = (await admin.get("/conhecimento")).text
    assert "Preços" in texto
    assert 'class="selo processando"' in texto
    assert "atualiza sozinha" in texto


async def test_texto_escrito_no_painel_vira_arquivo_e_indexa_de_verdade(
    admin: httpx.AsyncClient, ambiente: Any
) -> None:
    resposta = await admin.post(
        "/conhecimento/texto", data={"titulo": "Horários", "texto": "# Horários\nAbrimos às 8h."}
    )
    assert resposta.status_code == 303
    documento = (await _documentos(ambiente))[0]
    assert documento.arquivo is not None
    assert "Abrimos às 8h" in (ambiente.diretorio / documento.arquivo).read_text("utf-8")
    fabrica = ambiente.api.state.fabrica_sessoes
    await indexar_documento(fabrica, _GeradorFalso(), ambiente.diretorio, documento.id)
    assert (await _documentos(ambiente))[0].status == "pronto"
    async with fabrica() as sessao:
        trecho = await sessao.scalar(
            select(ConhecimentoTrecho).where(ConhecimentoTrecho.documento_id == documento.id)
        )
    assert trecho is not None
    assert "Abrimos às 8h" in trecho.texto


async def test_editar_o_texto_pede_nova_indexacao(admin: httpx.AsyncClient, ambiente: Any) -> None:
    await admin.post("/conhecimento/texto", data={"titulo": "T", "texto": "antes"})
    documento = (await _documentos(ambiente))[0]
    assert "antes" in (await admin.get(f"/conhecimento/{documento.id}/editar")).text
    resposta = await admin.post(
        f"/conhecimento/{documento.id}/editar", data={"titulo": "T2", "texto": "depois"}
    )
    assert resposta.status_code == 303
    novo = (await _documentos(ambiente))[0]
    assert novo.titulo == "T2"
    assert novo.arquivo is not None
    assert (ambiente.diretorio / novo.arquivo).read_text("utf-8") == "depois"
    assert ambiente.indexacao.pedidos.count(documento.id) == 2


async def test_arquivo_enviado_nao_e_editavel(admin: httpx.AsyncClient, ambiente: Any) -> None:
    await _enviar(admin)
    documento = (await _documentos(ambiente))[0]
    assert (await admin.get(f"/conhecimento/{documento.id}/editar")).status_code == 409
    assert "/editar" not in (await admin.get("/conhecimento")).text


async def test_texto_vazio_e_recusado(admin: httpx.AsyncClient, ambiente: Any) -> None:
    resposta = await admin.post("/conhecimento/texto", data={"titulo": "T", "texto": "   "})
    assert resposta.status_code == 422
    assert await _documentos(ambiente) == []


async def test_desativar_apagar_e_auditoria(admin: httpx.AsyncClient, ambiente: Any) -> None:
    await _enviar(admin)
    documento = (await _documentos(ambiente))[0]
    assert documento.arquivo is not None
    arquivo = ambiente.diretorio / documento.arquivo
    assert arquivo.exists()
    await admin.post(f"/conhecimento/{documento.id}/ativo", data={"ativo": "0"})
    assert (await _documentos(ambiente))[0].ativo is False
    await admin.post(f"/conhecimento/{documento.id}/apagar")
    assert await _documentos(ambiente) == []
    assert not arquivo.exists()
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        acoes = await sessao.scalars(
            select(Auditoria.acao).where(
                Auditoria.entidade == "conhecimento", Auditoria.clinica_id == ambiente.clinica_id
            )
        )
        assert sorted(acoes) == ["apagar", "atualizar", "criar"]


async def test_documento_inexistente(admin: httpx.AsyncClient) -> None:
    assert (await admin.post(f"/conhecimento/{UUID(int=1)}/apagar")).status_code == 404


async def test_testar_pergunta_mostra_os_trechos(admin: httpx.AsyncClient, ambiente: Any) -> None:
    ambiente.busca_trechos.append({"texto": "Limpeza custa <b>100</b>", "fonte": "Preços"})
    resposta = await admin.post("/conhecimento/testar", data={"pergunta": "Quanto custa?"})
    assert resposta.status_code == 200
    assert "Limpeza custa &lt;b&gt;100&lt;/b&gt;" in resposta.text
    assert "Preços" in resposta.text
    assert 'value="Quanto custa?"' in resposta.text
    assert str(ambiente.clinica_id) in ambiente.busca_pedidos[0].content.decode()


async def test_testar_pergunta_sem_resultado(admin: httpx.AsyncClient) -> None:
    resposta = await admin.post("/conhecimento/testar", data={"pergunta": "Marte?"})
    assert "não trouxe nenhum trecho" in resposta.text


async def test_busca_fora_do_ar_nao_derruba_a_tela(admin: httpx.AsyncClient, ambiente: Any) -> None:
    ambiente.busca_erro.append(500)
    resposta = await admin.post("/conhecimento/testar", data={"pergunta": "x"})
    assert resposta.status_code == 502
    assert "A busca está indisponível" in resposta.text


async def test_recepcao_nao_testa_pergunta(recepcao: httpx.AsyncClient, ambiente: Any) -> None:
    assert (await recepcao.post("/conhecimento/testar", data={"pergunta": "x"})).status_code == 403
    assert ambiente.busca_pedidos == []


def test_caminho_nao_sai_do_volume(tmp_path: Path) -> None:
    with pytest.raises(ErroDaApi):
        conhecimento.caminho_no_volume(tmp_path, "../fora.md")
