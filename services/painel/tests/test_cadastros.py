from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from clinica_api.horarios import listar_horarios_livres
from clinica_core.config import ConfigAgenda, ConfigTokens
from clinica_core.modelos import (
    Auditoria,
    Bloqueio,
    Clinica,
    Consultorio,
    Dentista,
    GradeTrabalho,
    Procedimento,
)

FUSO = ZoneInfo("America/Sao_Paulo")


async def _criar(cliente: httpx.AsyncClient, rota: str, **campos: Any) -> httpx.Response:
    resposta = await cliente.post(f"/cadastros/{rota}", data=campos)
    assert resposta.status_code == 303, resposta.text
    return resposta


async def _id(ambiente: Any, modelo: Any, nome: str) -> UUID:
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        id_ = await sessao.scalar(
            select(modelo.id).where(modelo.nome == nome, modelo.clinica_id == ambiente.clinica_id)
        )
    assert id_ is not None
    return UUID(str(id_))


@pytest.fixture
async def base(admin: httpx.AsyncClient, ambiente: Any) -> dict[str, UUID]:
    await _criar(admin, "dentistas", nome="Dra. Ana", especialidades="geral, canal", ativo="1")
    await _criar(admin, "consultorios", nome="Sala 1", ativo="1")
    return {
        "dentista": await _id(ambiente, Dentista, "Dra. Ana"),
        "consultorio": await _id(ambiente, Consultorio, "Sala 1"),
    }


def _grade(base: dict[str, UUID], inicio: str = "08:00", fim: str = "12:00") -> dict[str, str]:
    return {
        "dentista_id": str(base["dentista"]),
        "consultorio_id": str(base["consultorio"]),
        "dia_semana": "0",
        "inicio": inicio,
        "fim": fim,
    }


@pytest.mark.parametrize(
    "rota", ["dentistas", "grade", "procedimentos", "consultorios", "bloqueios"]
)
async def test_a_api_recusa_a_recepcao_nos_cadastros(ambiente: Any, rota: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ambiente.api), base_url="http://api"
    ) as api:
        resposta = await api.delete(
            f"/painel/{rota}/{uuid4()}",
            headers={**ConfigTokens().cabecalho_painel, "X-Usuario-Id": str(ambiente.recepcao_id)},
        )
    assert (resposta.status_code, resposta.json()["motivo"]) == (403, "sem_permissao")


async def test_recepcao_nao_cria_dentista_pela_api(
    ambiente: Any, recepcao: httpx.AsyncClient
) -> None:
    resposta = await recepcao.post("/cadastros/dentistas", data={"nome": "x", "ativo": "1"})
    assert resposta.status_code == 403
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        assert await sessao.scalar(select(Dentista.id).where(Dentista.nome == "x")) is None


async def test_crud_de_dentista_com_auditoria(
    admin: httpx.AsyncClient, base: dict[str, UUID], ambiente: Any
) -> None:
    id_ = base["dentista"]
    tela = (await admin.get("/cadastros/dentistas")).text
    assert "Dra. Ana" in tela
    assert "geral, canal" in tela
    assert 'value="geral, canal"' in (await admin.get(f"/cadastros/dentistas/{id_}")).text
    await _criar_edicao(admin, f"dentistas/{id_}", nome="Dra. Ana Lima", especialidades="geral")
    tela = (await admin.get("/cadastros/dentistas")).text
    assert "Dra. Ana Lima" in tela
    assert "Não" in tela
    apagar = await admin.post(f"/cadastros/dentistas/{id_}/apagar")
    assert apagar.status_code == 303
    assert "Dra. Ana" not in (await admin.get("/cadastros/dentistas")).text
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        acoes = await sessao.scalars(
            select(Auditoria.acao).where(
                Auditoria.entidade == "dentista", Auditoria.clinica_id == ambiente.clinica_id
            )
        )
        assert sorted(acoes) == ["apagar", "atualizar", "criar"]


async def _criar_edicao(cliente: httpx.AsyncClient, caminho: str, **campos: str) -> None:
    resposta = await cliente.post(f"/cadastros/{caminho}", data=campos)
    assert resposta.status_code == 303, resposta.text


async def test_dentista_em_uso_nao_e_apagado(
    admin: httpx.AsyncClient, base: dict[str, UUID]
) -> None:
    await _criar(admin, "grade", **_grade(base))
    resposta = await admin.post(f"/cadastros/dentistas/{base['dentista']}/apagar")
    assert resposta.status_code == 409
    assert "em uso" in resposta.text


async def test_grade_sobreposta_do_mesmo_dentista_e_recusada(
    admin: httpx.AsyncClient, base: dict[str, UUID]
) -> None:
    await _criar(admin, "grade", **_grade(base))
    resposta = await admin.post("/cadastros/grade", data=_grade(base, "11:00", "13:00"))
    assert resposta.status_code == 409
    assert "se sobrepõe" in resposta.text
    await _criar(admin, "grade", **_grade(base, "12:00", "13:00"))


async def test_grade_sobreposta_do_mesmo_consultorio_e_recusada(
    admin: httpx.AsyncClient, base: dict[str, UUID], ambiente: Any
) -> None:
    await _criar(admin, "grade", **_grade(base))
    await _criar(admin, "dentistas", nome="Dr. Beto", ativo="1")
    outro = await _id(ambiente, Dentista, "Dr. Beto")
    resposta = await admin.post(
        "/cadastros/grade", data={**_grade(base), "dentista_id": str(outro)}
    )
    assert resposta.status_code == 409


async def test_o_banco_recusa_a_sobreposicao_mesmo_fora_da_api(
    base: dict[str, UUID], ambiente: Any
) -> None:
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        for inicio, fim in ((time(8), time(12)), (time(11), time(13))):
            sessao.add(
                GradeTrabalho(
                    clinica_id=ambiente.clinica_id,
                    dentista_id=base["dentista"],
                    consultorio_id=base["consultorio"],
                    dia_semana=2,
                    inicio=inicio,
                    fim=fim,
                )
            )
        with pytest.raises(IntegrityError, match="grade_sem_conflito"):
            await sessao.commit()


async def test_outro_dia_nao_conflita(admin: httpx.AsyncClient, base: dict[str, UUID]) -> None:
    await _criar(admin, "grade", **_grade(base))
    await _criar(admin, "grade", **{**_grade(base), "dia_semana": "1"})
    tela = (await admin.get("/cadastros/grade")).text
    assert "Segunda" in tela
    assert "Terça" in tela
    assert "Dra. Ana" in tela
    assert "Sala 1" in tela


async def test_grade_com_fim_antes_do_inicio_e_recusada(
    admin: httpx.AsyncClient, base: dict[str, UUID]
) -> None:
    resposta = await admin.post("/cadastros/grade", data=_grade(base, "12:00", "08:00"))
    assert resposta.status_code == 422


async def test_grade_edita_e_apaga(
    admin: httpx.AsyncClient, base: dict[str, UUID], ambiente: Any
) -> None:
    await _criar(admin, "grade", **_grade(base))
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        id_ = await sessao.scalar(
            select(GradeTrabalho.id).where(GradeTrabalho.dentista_id == base["dentista"])
        )
    assert 'value="08:00"' in (await admin.get(f"/cadastros/grade/{id_}")).text
    await _criar_edicao(admin, f"grade/{id_}", **_grade(base, "09:00", "12:00"))
    assert "09:00" in (await admin.get("/cadastros/grade")).text
    assert (await admin.post(f"/cadastros/grade/{id_}/apagar")).status_code == 303


async def test_referencia_a_outra_clinica_e_recusada(
    admin: httpx.AsyncClient, base: dict[str, UUID], ambiente: Any
) -> None:
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        outra = Clinica(nome="O", telefone="+5511", endereco="R", politica_cancelamento_horas=24)
        sessao.add(outra)
        await sessao.flush()
        estranho = Dentista(clinica_id=outra.id, nome="Estranho")
        sessao.add(estranho)
        await sessao.commit()
        estranho_id = estranho.id
    resposta = await admin.post(
        "/cadastros/grade", data={**_grade(base), "dentista_id": str(estranho_id)}
    )
    assert resposta.status_code == 404
    assert (await admin.get(f"/cadastros/dentistas/{estranho_id}")).status_code == 404
    assert (await admin.post(f"/cadastros/dentistas/{estranho_id}/apagar")).status_code == 404


async def test_aviso_de_procedimento_sem_dentista(
    admin: httpx.AsyncClient, base: dict[str, UUID], ambiente: Any
) -> None:
    await _criar(
        admin,
        "procedimentos",
        nome="Limpeza",
        sinonimos="profilaxia, tartarectomia",
        duracao_min="30",
        intervalo_limpeza_min="10",
    )
    tela = (await admin.get("/cadastros/procedimentos")).text
    assert "Limpeza: sem dentista habilitado" in tela
    assert "profilaxia, tartarectomia" in tela
    id_ = await _id(ambiente, Procedimento, "Limpeza")
    await _criar_edicao(
        admin,
        f"procedimentos/{id_}",
        nome="Limpeza",
        duracao_min="30",
        intervalo_limpeza_min="10",
        dentistas_habilitados=str(base["dentista"]),
    )
    tela = (await admin.get("/cadastros/procedimentos")).text
    assert "sem dentista habilitado" not in tela
    edicao = (await admin.get(f"/cadastros/procedimentos/{id_}")).text
    assert f'value="{base["dentista"]}" checked' in edicao


async def test_procedimento_com_duracao_invalida(admin: httpx.AsyncClient) -> None:
    for duracao in ("0", "abc", ""):
        resposta = await admin.post(
            "/cadastros/procedimentos",
            data={"nome": "X", "duracao_min": duracao, "intervalo_limpeza_min": "0"},
        )
        assert resposta.status_code == 422, duracao


async def test_bloqueio_vale_no_fuso_da_clinica_e_tira_o_horario(
    admin: httpx.AsyncClient, base: dict[str, UUID], ambiente: Any
) -> None:
    await _criar(admin, "grade", **_grade(base))
    await _criar(
        admin,
        "procedimentos",
        nome="Limpeza",
        duracao_min="30",
        intervalo_limpeza_min="0",
        dentistas_habilitados=str(base["dentista"]),
    )
    hoje = date.today()
    dia = hoje + timedelta(days=14 - hoje.weekday())
    await _criar(
        admin,
        "bloqueios",
        dentista_id=str(base["dentista"]),
        inicio=f"{dia}T08:00",
        fim=f"{dia}T10:00",
        motivo="Dentista em congresso",
    )
    tela = (await admin.get("/cadastros/bloqueios")).text
    assert "Dentista em congresso" in tela
    assert f"{dia:%d/%m/%Y} 08:00" in tela
    procedimento = await _id(ambiente, Procedimento, "Limpeza")
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        horarios = await listar_horarios_livres(
            sessao,
            ambiente.clinica_id,
            procedimento,
            datetime.combine(dia, time.min, tzinfo=FUSO),
            datetime.combine(dia, time.max, tzinfo=FUSO),
            ConfigAgenda(antecedencia_minima_min=0),
            base["dentista"],
        )
    assert horarios[0].inicio.astimezone(FUSO).time() == time(10)


async def test_bloqueio_da_clinica_toda_e_edicao(admin: httpx.AsyncClient, ambiente: Any) -> None:
    amanha = date.today() + timedelta(days=1)
    await _criar(
        admin,
        "bloqueios",
        dentista_id="",
        inicio=f"{amanha}T08:00",
        fim=f"{amanha}T18:00",
        motivo="Feriado",
    )
    assert "Clínica toda" in (await admin.get("/cadastros/bloqueios")).text
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        id_ = await sessao.scalar(
            select(Bloqueio.id).where(
                Bloqueio.motivo == "Feriado", Bloqueio.clinica_id == ambiente.clinica_id
            )
        )
    tela = (await admin.get(f"/cadastros/bloqueios/{id_}")).text
    assert f'value="{amanha}T08:00"' in tela


async def test_bloqueio_com_periodo_invalido(admin: httpx.AsyncClient) -> None:
    resposta = await admin.post(
        "/cadastros/bloqueios",
        data={"inicio": "2030-01-02T10:00", "fim": "2030-01-02T08:00", "motivo": "x"},
    )
    assert resposta.status_code == 422
    vazio = await admin.post("/cadastros/bloqueios", data={"motivo": "x"})
    assert vazio.status_code == 422


async def test_cadastro_inexistente(admin: httpx.AsyncClient) -> None:
    assert (await admin.get("/cadastros/nao-existe")).status_code == 404
    assert (await admin.get(f"/cadastros/dentistas/{uuid4()}")).status_code == 404


async def test_usuario_nao_tem_rota_de_apagar(admin: httpx.AsyncClient, ambiente: Any) -> None:
    resposta = await admin.post(f"/cadastros/usuarios/{ambiente.recepcao_id}/apagar")
    assert resposta.status_code == 405


async def test_migration_nao_deixa_lixo_de_sql(ambiente: Any) -> None:
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        nomes = await sessao.scalars(
            text("SELECT conname FROM pg_constraint WHERE conname LIKE 'grade_sem_conflito%'")
        )
        assert sorted(nomes) == ["grade_sem_conflito_consultorio", "grade_sem_conflito_dentista"]
