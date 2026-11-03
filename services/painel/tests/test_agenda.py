import re
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from clinica_core.eventos import EventoAgendamento
from clinica_core.modelos import (
    Agendamento,
    Auditoria,
    Consultorio,
    Dentista,
    GradeTrabalho,
    Paciente,
    Procedimento,
)
from clinica_painel.agenda import gerar_avisos
from clinica_painel.eventos import Distribuidor

FUSO = ZoneInfo("America/Sao_Paulo")


class Dados:
    dentista: UUID
    procedimento: UUID
    paciente: UUID
    dia: date

    def inicio(self, hora: int, minuto: int = 0) -> str:
        return datetime.combine(self.dia, time(hora, minuto), tzinfo=FUSO).isoformat()


@pytest.fixture
async def dados(ambiente: Any) -> Dados:
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        cid = ambiente.clinica_id
        dentista = Dentista(clinica_id=cid, nome="Dra. Ana")
        consultorio = Consultorio(clinica_id=cid, nome="Sala 1")
        paciente = Paciente(
            clinica_id=cid,
            nome="Maria Souza",
            telefone_e164="+5511900000001",
            data_nascimento=date(1990, 1, 1),
        )
        sessao.add_all([dentista, consultorio, paciente])
        await sessao.flush()
        procedimento = Procedimento(
            clinica_id=cid, nome="Limpeza", duracao_min=30, dentistas_habilitados=[dentista.id]
        )
        sessao.add(procedimento)
        hoje = datetime.now(FUSO).date()
        dia = hoje + timedelta(days=14 - hoje.weekday())
        sessao.add(
            GradeTrabalho(
                clinica_id=cid,
                dentista_id=dentista.id,
                consultorio_id=consultorio.id,
                dia_semana=0,
                inicio=time(8),
                fim=time(12),
            )
        )
        await sessao.commit()
        resultado = Dados()
        resultado.dentista = dentista.id
        resultado.procedimento = procedimento.id
        resultado.paciente = paciente.id
        resultado.dia = dia
    return resultado


def _formulario(d: Dados, inicio: str, chave: str = "chave-1") -> dict[str, str]:
    return {
        "chave": chave,
        "procedimento_id": str(d.procedimento),
        "dentista_id": str(d.dentista),
        "data": d.dia.isoformat(),
        "inicio": inicio,
        "paciente_id": str(d.paciente),
    }


async def _agendar(cliente: httpx.AsyncClient, d: Dados, hora: int = 9, chave: str = "c1") -> str:
    resposta = await cliente.post("/agenda/nova", data=_formulario(d, d.inicio(hora), chave))
    assert resposta.status_code == 303, resposta.text
    return await _id_da_consulta(cliente, d)


async def _id_da_consulta(cliente: httpx.AsyncClient, d: Dados) -> str:
    grade = await cliente.get("/agenda/grade", params={"dia": d.dia.isoformat()})
    ids: list[str] = re.findall(r"/agenda/consulta/([0-9a-f-]{36})", grade.text)
    return ids[-1]


async def _status(ambiente: Any, id_: str) -> str:
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        return str(
            await sessao.scalar(select(Agendamento.status).where(Agendamento.id == UUID(id_)))
        )


async def test_agenda_exige_login(anonimo: httpx.AsyncClient) -> None:
    assert (await anonimo.get("/agenda")).status_code == 303
    assert (await anonimo.get("/agenda/eventos")).status_code == 303


async def test_tela_da_agenda_abre_com_as_atualizacoes_ao_vivo(
    recepcao: httpx.AsyncClient, dados: Dados
) -> None:
    texto = (await recepcao.get("/agenda")).text
    assert "Dra. Ana" in texto
    assert 'EventSource("/agenda/eventos")' in texto
    assert "atualizar from:body" in texto


async def test_recepcao_agenda_e_a_consulta_aparece_com_status(
    recepcao: httpx.AsyncClient, dados: Dados, ambiente: Any
) -> None:
    id_ = await _agendar(recepcao, dados)
    grade = (await recepcao.get("/agenda/grade", params={"dia": dados.dia.isoformat()})).text
    assert "09:00" in grade
    assert "Maria Souza" in grade
    assert "Limpeza" in grade
    assert "confirmado" in grade
    assert await _status(ambiente, id_) == "confirmado"
    assert [e.tipo for e in ambiente.publicador.eventos_publicados] == ["criado", "confirmado"]


async def test_visao_semanal_mostra_os_sete_dias(recepcao: httpx.AsyncClient, dados: Dados) -> None:
    await _agendar(recepcao, dados)
    meio = dados.dia + timedelta(days=3)
    grade = (
        await recepcao.get("/agenda/grade", params={"dia": meio.isoformat(), "visao": "semana"})
    ).text
    assert grade.count('class="dia"') == 7
    assert "Maria Souza" in grade


async def test_filtro_por_dentista(
    recepcao: httpx.AsyncClient, dados: Dados, ambiente: Any
) -> None:
    await _agendar(recepcao, dados)
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        outro = Dentista(clinica_id=ambiente.clinica_id, nome="Dr. Beto")
        sessao.add(outro)
        await sessao.commit()
        outro_id = outro.id
    dia = dados.dia.isoformat()
    assert "Maria" in (await recepcao.get("/agenda/grade", params={"dia": dia})).text
    grade = await recepcao.get("/agenda/grade", params={"dia": dia, "dentista": str(outro_id)})
    assert "Maria" not in grade.text


async def test_horarios_livres_saem_da_api(recepcao: httpx.AsyncClient, dados: Dados) -> None:
    parametros = {
        "procedimento_id": str(dados.procedimento),
        "dentista_id": str(dados.dentista),
        "data": dados.dia.isoformat(),
    }
    texto = (await recepcao.get("/agenda/horarios", params=parametros)).text
    assert "08:00" in texto
    await _agendar(recepcao, dados, hora=8)
    texto = (await recepcao.get("/agenda/horarios", params=parametros)).text
    assert "08:00" not in texto
    assert "08:30" in texto


async def test_horarios_sem_escolha_pede_para_escolher(recepcao: httpx.AsyncClient) -> None:
    assert "Escolha o procedimento" in (await recepcao.get("/agenda/horarios")).text


async def test_mesmo_horario_e_recusado_e_mostra_a_mensagem(
    recepcao: httpx.AsyncClient, admin: httpx.AsyncClient, dados: Dados
) -> None:
    await _agendar(recepcao, dados)
    resposta = await admin.post("/agenda/nova", data=_formulario(dados, dados.inicio(9), "c2"))
    assert resposta.status_code == 409
    assert "acabou de ser ocupado" in resposta.text


async def test_reenviar_o_formulario_nao_duplica(
    recepcao: httpx.AsyncClient, dados: Dados, ambiente: Any
) -> None:
    formulario = _formulario(dados, dados.inicio(9), "mesma-chave")
    assert (await recepcao.post("/agenda/nova", data=formulario)).status_code == 303
    # O hold já virou consulta, então segurar de novo encontra o horário ocupado.
    assert (await recepcao.post("/agenda/nova", data=formulario)).status_code == 409
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        total = (
            await sessao.scalars(
                select(Agendamento).where(Agendamento.dentista_id == dados.dentista)
            )
        ).all()
    assert len(total) == 1


async def test_sem_horario_ou_paciente_nao_agenda(
    recepcao: httpx.AsyncClient, dados: Dados
) -> None:
    formulario = {**_formulario(dados, ""), "paciente_id": ""}
    resposta = await recepcao.post("/agenda/nova", data=formulario)
    assert resposta.status_code == 422
    assert "Escolha o horário e o paciente" in resposta.text


async def test_remarcar_pelo_painel(
    recepcao: httpx.AsyncClient, dados: Dados, ambiente: Any
) -> None:
    antigo = await _agendar(recepcao, dados, hora=9)
    resposta = await recepcao.post(
        f"/agenda/consulta/{antigo}/remarcar",
        data={
            "chave": "r1",
            "procedimento_id": str(dados.procedimento),
            "dentista_id": str(dados.dentista),
            "inicio": dados.inicio(10),
        },
    )
    assert resposta.status_code == 303, resposta.text
    assert await _status(ambiente, antigo) == "cancelado"
    grade = (await recepcao.get("/agenda/grade", params={"dia": dados.dia.isoformat()})).text
    assert "10:00" in grade


async def test_cancelar_pelo_painel_libera_o_horario(
    recepcao: httpx.AsyncClient, dados: Dados, ambiente: Any
) -> None:
    id_ = await _agendar(recepcao, dados)
    resposta = await recepcao.post(
        f"/agenda/consulta/{id_}/cancelar", data={"chave": "x", "motivo": "viagem"}
    )
    assert resposta.status_code == 303
    assert await _status(ambiente, id_) == "cancelado"
    tela = (await recepcao.get(f"/agenda/consulta/{id_}")).text
    assert "viagem" in tela
    assert "Remarcar" not in tela
    await _agendar(recepcao, dados, chave="de-novo")


@pytest.mark.parametrize("status", ["faltou", "concluido"])
async def test_marcar_falta_ou_conclusao(
    recepcao: httpx.AsyncClient, dados: Dados, ambiente: Any, status: str
) -> None:
    id_ = await _agendar(recepcao, dados)
    resposta = await recepcao.post(f"/agenda/consulta/{id_}/status", data={"status": status})
    assert resposta.status_code == 303
    assert await _status(ambiente, id_) == status
    assert ambiente.publicador.eventos_publicados[-1].tipo == "atualizado"
    # Só uma consulta confirmada muda de status.
    repetida = await recepcao.post(f"/agenda/consulta/{id_}/status", data={"status": status})
    assert repetida.status_code == 409


async def test_status_invalido_e_recusado(recepcao: httpx.AsyncClient, dados: Dados) -> None:
    id_ = await _agendar(recepcao, dados)
    resposta = await recepcao.post(f"/agenda/consulta/{id_}/status", data={"status": "cancelado"})
    assert resposta.status_code == 422


async def test_escritas_da_agenda_ficam_na_auditoria_com_o_usuario(
    recepcao: httpx.AsyncClient, dados: Dados, ambiente: Any
) -> None:
    id_ = await _agendar(recepcao, dados)
    await recepcao.post(f"/agenda/consulta/{id_}/cancelar", data={"chave": "x"})
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        registros = (
            await sessao.scalars(
                select(Auditoria).where(
                    Auditoria.entidade == "agendamento",
                    Auditoria.clinica_id == ambiente.clinica_id,
                )
            )
        ).all()
    assert sorted(r.acao for r in registros) == ["cancelar", "confirmar", "segurar"]
    assert {r.usuario_id for r in registros} == {ambiente.recepcao_id}


async def test_buscar_paciente_por_nome_e_telefone(
    recepcao: httpx.AsyncClient, dados: Dados
) -> None:
    for busca in ("maria", "SOUZA", "900000001"):
        texto = (await recepcao.get("/agenda/pacientes", params={"busca": busca})).text
        assert "Maria Souza" in texto, busca
    assert "Maria" not in (await recepcao.get("/agenda/pacientes", params={"busca": "zzz"})).text


async def test_cadastro_de_paciente_normaliza_o_telefone_e_audita_sem_dados(
    recepcao: httpx.AsyncClient, ambiente: Any
) -> None:
    resposta = await recepcao.post(
        "/cadastros/pacientes",
        data={
            "nome": "Ana Lima",
            "telefone_e164": "(11) 98888-7777",
            "data_nascimento": "1980-02-03",
        },
    )
    assert resposta.status_code == 303, resposta.text
    assert "+5511988887777" in (await recepcao.get("/cadastros/pacientes")).text
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        registros = (
            await sessao.scalars(select(Auditoria).where(Auditoria.entidade == "paciente"))
        ).all()
    assert {"criar", "ver"} <= {r.acao for r in registros}
    assert "Ana" not in str([(r.antes, r.depois) for r in registros])
    assert "98888" not in str([(r.antes, r.depois) for r in registros])


async def test_paciente_com_telefone_invalido_ou_duplicado(
    recepcao: httpx.AsyncClient, dados: Dados
) -> None:
    invalido = await recepcao.post(
        "/cadastros/pacientes",
        data={"nome": "Ana", "telefone_e164": "123", "data_nascimento": "1980-02-03"},
    )
    assert invalido.status_code == 422
    assert "Telefone inválido" in invalido.text
    duplicado = await recepcao.post(
        "/cadastros/pacientes",
        data={
            "nome": "Maria Souza",
            "telefone_e164": "+5511900000001",
            "data_nascimento": "1990-01-01",
        },
    )
    assert duplicado.status_code == 409
    assert "Já existe esse paciente" in duplicado.text


async def test_avisos_so_da_propria_clinica_e_sem_dados() -> None:

    fonte = Distribuidor()
    clinica = uuid4()
    entregues: list[str] = []
    verificacoes = 0

    async def desconectou() -> bool:
        nonlocal verificacoes
        verificacoes += 1
        return verificacoes > 2

    def evento(clinica_id: UUID) -> EventoAgendamento:
        return EventoAgendamento(
            tipo="confirmado",
            agendamento_id=uuid4(),
            clinica_id=clinica_id,
            dentista_id=uuid4(),
            consultorio_id=uuid4(),
            status="confirmado",
            inicio=datetime.now(FUSO),
            fim_periodo=datetime.now(FUSO),
        )

    gerador = gerar_avisos(fonte, str(clinica), desconectou, batimento_s=0.01)
    primeira = await anext(gerador)
    assert primeira == ": vivo\n\n"
    fonte.distribuir(evento(uuid4()))
    fonte.distribuir(evento(clinica))
    entregues.append(await anext(gerador))
    assert entregues == ["data: confirmado\n\n"]
    await gerador.aclose()
    assert fonte._filas == {}
