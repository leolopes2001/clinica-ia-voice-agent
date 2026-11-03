from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from clinica_core.modelos import Auditoria, Chamada, PedidoRetorno

TRANSCRICAO = "Paciente: <script>alert(1)</script> tenho dor\nRecepcionista: Certo."


async def _chamada(ambiente: Any, **campos: Any) -> UUID:
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        chamada = Chamada(clinica_id=ambiente.clinica_id, sala=f"s-{uuid4()}", **campos)
        sessao.add(chamada)
        await sessao.commit()
        return chamada.id


async def _auditoria(ambiente: Any, entidade: str) -> list[Auditoria]:
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        return list(
            await sessao.scalars(
                select(Auditoria).where(
                    Auditoria.entidade == entidade, Auditoria.clinica_id == ambiente.clinica_id
                )
            )
        )


async def test_lista_mostra_telefone_duracao_desfecho_sem_a_transcricao(
    admin: httpx.AsyncClient, ambiente: Any
) -> None:
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        agora = (await sessao.execute(select(func.now()))).scalar_one()
    await _chamada(
        ambiente,
        telefone="+5511900000001",
        inicio=agora,
        fim=agora + timedelta(seconds=125),
        desfecho="agendou",
        consentimento_em=agora,
        transcricao=TRANSCRICAO,
    )
    texto = (await admin.get("/chamadas")).text
    assert "+5511900000001" in texto
    assert "2 min 5 s" in texto
    assert "Agendou" in texto
    assert "Aceitou" in texto
    assert "tenho dor" not in texto
    assert [r.acao for r in await _auditoria(ambiente, "chamada")] == ["ver"]


async def test_filtro_por_desfecho(admin: httpx.AsyncClient, ambiente: Any) -> None:
    await _chamada(ambiente, telefone="+5511900000001", desfecho="agendou")
    await _chamada(ambiente, telefone="+5511900000002", desfecho="duvida")
    texto = (await admin.get("/chamadas", params={"desfecho": "duvida"})).text
    assert "+5511900000002" in texto
    assert "+5511900000001" not in texto


async def test_transcricao_aparece_escapada_e_grava_quem_leu(
    admin: httpx.AsyncClient, ambiente: Any
) -> None:
    id_ = await _chamada(ambiente, telefone="+5511900000001", transcricao=TRANSCRICAO)
    texto = (await admin.get(f"/chamadas/{id_}")).text
    assert "tenho dor" in texto
    assert "<script>alert" not in texto
    registros = await _auditoria(ambiente, "chamada")
    assert [(r.acao, r.usuario_id) for r in registros] == [("ver_transcricao", ambiente.admin_id)]
    assert "dor" not in str(registros[0].depois)


async def test_chamada_sem_transcricao_explica_o_motivo(
    admin: httpx.AsyncClient, ambiente: Any
) -> None:
    id_ = await _chamada(ambiente, telefone="+5511900000001")
    assert "Sem transcrição" in (await admin.get(f"/chamadas/{id_}")).text


@pytest.mark.parametrize("caminho", ["/chamadas", f"/chamadas/{uuid4()}"])
async def test_recepcao_nao_ve_chamadas(recepcao: httpx.AsyncClient, caminho: str) -> None:
    assert (await recepcao.get(caminho)).status_code == 403


async def test_chamada_inexistente(admin: httpx.AsyncClient) -> None:
    assert (await admin.get(f"/chamadas/{uuid4()}")).status_code == 404


async def _retorno(ambiente: Any) -> UUID:
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        pedido = PedidoRetorno(
            clinica_id=ambiente.clinica_id,
            protocolo=uuid4().hex[:8],
            telefone="+5511900000001",
            motivo="Quer falar sobre o orçamento",
        )
        sessao.add(pedido)
        await sessao.commit()
        return pedido.id


async def test_recepcao_ve_e_conclui_o_pedido_de_retorno(
    recepcao: httpx.AsyncClient, ambiente: Any
) -> None:
    id_ = await _retorno(ambiente)
    texto = (await recepcao.get("/retornos")).text
    assert "Quer falar sobre o orçamento" in texto
    assert f"/retornos/{id_}/concluir" in texto
    assert (await recepcao.post(f"/retornos/{id_}/concluir")).status_code == 303
    assert f"/retornos/{id_}/concluir" not in (await recepcao.get("/retornos")).text
    assert "concluir" in [r.acao for r in await _auditoria(ambiente, "pedido_retorno")]


async def test_pedido_de_outra_clinica_nao_e_concluido(recepcao: httpx.AsyncClient) -> None:
    assert (await recepcao.post(f"/retornos/{uuid4()}/concluir")).status_code == 404
