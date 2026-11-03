import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from clinica_api.app import criar_app
from clinica_api.publicacao import PublicadorRabbit
from clinica_api.travas import travar
from clinica_core.config import ConfigAgenda, ConfigPostgres, ConfigRabbit, ConfigTokens
from clinica_core.db import criar_fabrica_sessoes
from clinica_core.eventos import EventoAgendamento
from clinica_core.mensageria import abrir_canal, conectar, declarar_fila_painel
from clinica_core.modelos import (
    Agendamento,
    Bloqueio,
    Chamada,
    Clinica,
    Consultorio,
    Dentista,
    GradeTrabalho,
    Idempotencia,
    Paciente,
    Procedimento,
)

FUSO = ZoneInfo("America/Sao_Paulo")


class _TokenDoContexto(httpx.Auth):
    """O token que cada serviço usa: chamada é do mcp-clinica, o resto é do painel."""

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        tokens = ConfigTokens()
        cabecalho = (
            tokens.cabecalho_servicos
            if "X-Chamada-Id" in request.headers
            else tokens.cabecalho_painel
        )
        request.headers.update(cabecalho)
        yield request


@dataclass
class Cenario:
    clinica_id: UUID
    dentista_a: UUID
    dentista_b: UUID
    consultorio_1: UUID
    consultorio_2: UUID
    procedimento_id: UUID
    paciente_id: UUID
    # Uma segunda-feira futura, com o dentista A no consultório 1 e o B no 2, das 8h às 12h.
    dia: date

    def h(self, hora: int, minuto: int = 0) -> datetime:
        return datetime.combine(self.dia, time(hora, minuto), tzinfo=FUSO)

    @property
    def painel(self) -> dict[str, str]:
        return {"X-Clinica-Id": str(self.clinica_id)}


class PublicadorFalso:
    def __init__(self) -> None:
        self.holds: list[tuple[UUID, timedelta]] = []
        self.eventos_publicados: list[EventoAgendamento] = []

    async def abrir(self) -> None:
        pass

    async def fechar(self) -> None:
        pass

    async def hold_criado(self, hold_id: UUID, duracao: timedelta) -> None:
        self.holds.append((hold_id, duracao))

    async def eventos(self, eventos: Sequence[EventoAgendamento]) -> None:
        self.eventos_publicados.extend(eventos)


@pytest.fixture
def publicador() -> PublicadorFalso:
    return PublicadorFalso()


@pytest.fixture
async def app(banco: str, publicador: PublicadorFalso) -> AsyncIterator[FastAPI]:
    app = criar_app(ConfigPostgres(db=banco), ConfigAgenda(antecedencia_minima_min=0), publicador)
    yield app
    await app.state.engine.dispose()


@pytest.fixture
async def cliente(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transporte = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transporte, base_url="http://api", auth=_TokenDoContexto()
    ) as cliente:
        yield cliente


@pytest.fixture
def fabrica(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    fabrica: async_sessionmaker[AsyncSession] = app.state.fabrica_sessoes
    return fabrica


@pytest.fixture
async def c(fabrica: async_sessionmaker[AsyncSession]) -> Cenario:
    async with fabrica() as sessao:
        clinica = Clinica(nome="C", telefone="+5511", endereco="R", politica_cancelamento_horas=24)
        sessao.add(clinica)
        await sessao.flush()
        dentista_a = Dentista(clinica_id=clinica.id, nome="A")
        dentista_b = Dentista(clinica_id=clinica.id, nome="B")
        consultorio_1 = Consultorio(clinica_id=clinica.id, nome="1")
        consultorio_2 = Consultorio(clinica_id=clinica.id, nome="2")
        paciente = Paciente(
            clinica_id=clinica.id,
            nome="Maria",
            telefone_e164="+5511999990000",
            data_nascimento=date(1990, 1, 1),
        )
        sessao.add_all([dentista_a, dentista_b, consultorio_1, consultorio_2, paciente])
        await sessao.flush()
        procedimento = Procedimento(
            clinica_id=clinica.id,
            nome="Avaliação",
            duracao_min=30,
            dentistas_habilitados=[dentista_a.id, dentista_b.id],
        )
        sessao.add(procedimento)
        hoje = (await sessao.execute(select(func.now()))).scalar_one().astimezone(FUSO).date()
        dia = hoje + timedelta(days=14 - hoje.weekday())
        for dentista, consultorio in ((dentista_a, consultorio_1), (dentista_b, consultorio_2)):
            sessao.add(
                GradeTrabalho(
                    clinica_id=clinica.id,
                    dentista_id=dentista.id,
                    consultorio_id=consultorio.id,
                    dia_semana=dia.weekday(),
                    inicio=time(8),
                    fim=time(12),
                )
            )
        await sessao.commit()
        return Cenario(
            clinica.id,
            dentista_a.id,
            dentista_b.id,
            consultorio_1.id,
            consultorio_2.id,
            procedimento.id,
            paciente.id,
            dia,
        )


async def _chamada(
    fabrica: async_sessionmaker[AsyncSession],
    c: Cenario,
    paciente: UUID | None = None,
    verificada: bool = False,
) -> dict[str, str]:
    async with fabrica() as sessao:
        chamada = Chamada(
            clinica_id=c.clinica_id,
            telefone="+5511999990000",
            sala=f"call-{uuid4()}",
            paciente_id=paciente,
            identidade_verificada_em=func.now() if verificada else None,
            consentimento_em=func.now(),
        )
        sessao.add(chamada)
        await sessao.commit()
        return {"X-Chamada-Id": str(chamada.id)}


def _chave() -> dict[str, str]:
    return {"Idempotency-Key": uuid4().hex}


async def _segurar(
    cliente: httpx.AsyncClient,
    c: Cenario,
    cabecalhos: dict[str, str],
    inicio: datetime | None = None,
    dentista: UUID | None = None,
    chave: str | None = None,
) -> httpx.Response:
    return await cliente.post(
        "/holds",
        headers={**cabecalhos, "Idempotency-Key": chave or uuid4().hex},
        json={
            "procedimento_id": str(c.procedimento_id),
            "dentista_id": str(dentista or c.dentista_a),
            "inicio": (inicio or c.h(9)).isoformat(),
        },
    )


async def _hold(
    cliente: httpx.AsyncClient,
    c: Cenario,
    cabecalhos: dict[str, str],
    inicio: datetime | None = None,
) -> str:
    resposta = await _segurar(cliente, c, cabecalhos, inicio)
    assert resposta.status_code == 201, resposta.text
    hold_id: str = resposta.json()["hold_id"]
    return hold_id


async def _confirmar(
    cliente: httpx.AsyncClient,
    hold_id: str,
    cabecalhos: dict[str, str],
    paciente: UUID | None = None,
    chave: str | None = None,
) -> httpx.Response:
    corpo = {} if paciente is None else {"paciente_id": str(paciente)}
    return await cliente.post(
        f"/holds/{hold_id}/confirmar",
        headers={**cabecalhos, "Idempotency-Key": chave or uuid4().hex},
        json=corpo,
    )


async def _consulta(cliente: httpx.AsyncClient, c: Cenario, inicio: datetime | None = None) -> str:
    """Consulta confirmada pelo painel, do paciente do cenário."""
    hold_id = await _hold(cliente, c, c.painel, inicio)
    resposta = await _confirmar(cliente, hold_id, c.painel, c.paciente_id)
    assert resposta.status_code == 200, resposta.text
    return hold_id


async def _ler(fabrica: async_sessionmaker[AsyncSession], agendamento_id: str) -> Agendamento:
    async with fabrica() as sessao:
        return await sessao.get_one(Agendamento, UUID(agendamento_id))


async def _vencer(fabrica: async_sessionmaker[AsyncSession], hold_id: str) -> None:
    async with fabrica() as sessao:
        await sessao.execute(
            update(Agendamento)
            .where(Agendamento.id == UUID(hold_id))
            .values(expira_em=func.now() - timedelta(seconds=1))
        )
        await sessao.commit()


async def _inicios_livres(cliente: httpx.AsyncClient, c: Cenario) -> set[datetime]:
    resposta = await cliente.get(
        "/horarios-livres",
        headers=c.painel,
        params={
            "procedimento_id": str(c.procedimento_id),
            "a_partir_de": c.h(0).isoformat(),
            "ate": c.h(23).isoformat(),
            "dentista_id": str(c.dentista_a),
        },
    )
    assert resposta.status_code == 200
    return {datetime.fromisoformat(h["inicio"]) for h in resposta.json()["horarios"]}


Fabrica = async_sessionmaker[AsyncSession]


# ---- Saúde e contexto


async def test_saude(cliente: httpx.AsyncClient) -> None:
    resposta = await cliente.get("/saude")
    assert resposta.status_code == 200
    assert resposta.json() == {"status": "ok"}


async def test_sem_contexto_e_recusado(cliente: httpx.AsyncClient, c: Cenario) -> None:
    resposta = await _segurar(cliente, c, {})
    assert resposta.status_code == 400
    assert resposta.json()["motivo"] == "contexto_ausente"


async def test_chamada_inexistente_e_recusada(cliente: httpx.AsyncClient, c: Cenario) -> None:
    resposta = await _segurar(cliente, c, {"X-Chamada-Id": str(uuid4())})
    assert resposta.status_code == 404
    assert resposta.json()["motivo"] == "chamada_nao_encontrada"


async def test_clinica_inexistente_e_recusada(cliente: httpx.AsyncClient, c: Cenario) -> None:
    resposta = await _segurar(cliente, c, {"X-Clinica-Id": str(uuid4())})
    assert resposta.status_code == 404
    assert resposta.json()["motivo"] == "clinica_nao_encontrada"


async def test_clinica_diferente_da_chamada_e_recusada(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    cabecalhos = await _chamada(fabrica, c)
    resposta = await _segurar(cliente, c, {**cabecalhos, "X-Clinica-Id": str(uuid4())})
    assert resposta.status_code == 403
    assert resposta.json()["motivo"] == "contexto_divergente"


# ---- Listar


async def test_listar_devolve_candidatos(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    resposta = await cliente.get(
        "/horarios-livres",
        headers=await _chamada(fabrica, c),
        params={
            "procedimento_id": str(c.procedimento_id),
            "a_partir_de": c.h(8).isoformat(),
            "ate": c.h(8, 15).isoformat(),
        },
    )
    assert resposta.status_code == 200
    horarios = resposta.json()["horarios"]
    assert {h["dentista_id"] for h in horarios} == {str(c.dentista_a), str(c.dentista_b)}
    assert len(horarios) == 4
    primeiro = horarios[0]
    assert datetime.fromisoformat(primeiro["inicio"]) == c.h(8)
    assert datetime.fromisoformat(primeiro["fim"]) == c.h(8, 30)


async def test_listar_recusa_data_sem_fuso(cliente: httpx.AsyncClient, c: Cenario) -> None:
    resposta = await cliente.get(
        "/horarios-livres",
        headers=c.painel,
        params={
            "procedimento_id": str(c.procedimento_id),
            "a_partir_de": "2030-01-01T08:00:00",
            "ate": "2030-01-01T09:00:00",
        },
    )
    assert resposta.status_code == 422


async def test_listar_procedimento_de_outra_clinica(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    async with fabrica() as sessao:
        outra = Clinica(nome="O", telefone="+5521", endereco="R", politica_cancelamento_horas=24)
        sessao.add(outra)
        await sessao.commit()
    resposta = await cliente.get(
        "/horarios-livres",
        headers={"X-Clinica-Id": str(outra.id)},
        params={
            "procedimento_id": str(c.procedimento_id),
            "a_partir_de": c.h(8).isoformat(),
            "ate": c.h(9).isoformat(),
        },
    )
    assert resposta.status_code == 404
    assert resposta.json()["motivo"] == "procedimento_nao_encontrado"


# ---- Segurar


async def test_segurar_pela_ligacao(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    cabecalhos = await _chamada(fabrica, c)
    resposta = await _segurar(cliente, c, cabecalhos)
    assert resposta.status_code == 201
    corpo = resposta.json()
    assert corpo["ok"] is True
    hold = await _ler(fabrica, corpo["hold_id"])
    assert hold.status == "segurado"
    assert hold.origem == "voz"
    assert str(hold.chamada_id) == cabecalhos["X-Chamada-Id"]
    assert hold.paciente_id is None
    assert hold.consultorio_id == c.consultorio_1
    assert hold.periodo.lower == c.h(9)
    assert hold.periodo.upper == c.h(9, 30)
    assert hold.expira_em is not None
    async with fabrica() as sessao:
        agora = (await sessao.execute(select(func.now()))).scalar_one()
    assert timedelta(minutes=2, seconds=50) < hold.expira_em - agora <= timedelta(minutes=3)


async def test_segurar_pelo_painel(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    hold = await _ler(fabrica, await _hold(cliente, c, c.painel))
    assert hold.origem == "painel"
    assert hold.chamada_id is None


async def test_duracao_do_hold_vem_da_configuracao(
    banco: str, c: Cenario, fabrica: Fabrica
) -> None:
    app = criar_app(
        ConfigPostgres(db=banco),
        ConfigAgenda(antecedencia_minima_min=0, duracao_hold_min=5),
    )
    transporte = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transporte, base_url="http://api", auth=_TokenDoContexto()
    ) as cliente:
        hold = await _ler(fabrica, await _hold(cliente, c, c.painel))
    await app.state.engine.dispose()
    assert hold.expira_em is not None
    async with fabrica() as sessao:
        agora = (await sessao.execute(select(func.now()))).scalar_one()
    assert timedelta(minutes=4, seconds=50) < hold.expira_em - agora <= timedelta(minutes=5)


async def test_horario_ocupado_traz_alternativas_no_mesmo_dentista(
    cliente: httpx.AsyncClient, c: Cenario
) -> None:
    await _hold(cliente, c, c.painel)
    resposta = await _segurar(cliente, c, c.painel)
    assert resposta.status_code == 409
    corpo = resposta.json()
    assert corpo["ok"] is False
    assert corpo["motivo"] == "ocupado"
    inicios = {datetime.fromisoformat(a["inicio"]) for a in corpo["alternativas"]}
    assert c.h(9, 30) in inicios
    assert c.h(9) not in inicios
    assert {a["dentista_id"] for a in corpo["alternativas"]} == {str(c.dentista_a)}


async def test_alternativas_cobrem_ate_7_dias(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    await _hold(cliente, c, c.painel)
    corpo = (await _segurar(cliente, c, c.painel)).json()
    dias = {
        datetime.fromisoformat(a["inicio"]).astimezone(FUSO).date() for a in corpo["alternativas"]
    }
    # A grade do cenário só tem segundas: o dia pedido e a segunda seguinte.
    assert dias == {c.dia, c.dia + timedelta(days=7)}


@pytest.mark.parametrize(
    ("hora", "minuto"),
    [
        (7, 0),  # antes da grade
        (9, 7),  # fora dos múltiplos de 15 minutos
        (11, 45),  # não cabe no fim do turno
        (13, 0),  # depois da grade
    ],
)
async def test_horario_fora_da_grade_e_indisponivel(
    cliente: httpx.AsyncClient, c: Cenario, hora: int, minuto: int
) -> None:
    resposta = await _segurar(cliente, c, c.painel, c.h(hora, minuto))
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "indisponivel"
    assert resposta.json()["alternativas"]


async def test_horario_bloqueado_e_indisponivel(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    async with fabrica() as sessao:
        sessao.add(
            Bloqueio(clinica_id=c.clinica_id, periodo=Range(c.h(9), c.h(10)), motivo="Reunião")
        )
        await sessao.commit()
    resposta = await _segurar(cliente, c, c.painel)
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "indisponivel"


async def test_dentista_nao_habilitado_e_indisponivel(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    async with fabrica() as sessao:
        procedimento = await sessao.get_one(Procedimento, c.procedimento_id)
        procedimento.dentistas_habilitados = [c.dentista_b]
        await sessao.commit()
    resposta = await _segurar(cliente, c, c.painel)
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "indisponivel"
    assert resposta.json()["alternativas"] == []


async def test_dentista_de_outra_clinica_e_indisponivel(
    cliente: httpx.AsyncClient, c: Cenario
) -> None:
    resposta = await _segurar(cliente, c, c.painel, dentista=uuid4())
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "indisponivel"


async def test_antecedencia_minima_torna_indisponivel(
    banco: str, c: Cenario, fabrica: Fabrica
) -> None:
    app = criar_app(ConfigPostgres(db=banco), ConfigAgenda(antecedencia_minima_min=60 * 24 * 60))
    transporte = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transporte, base_url="http://api", auth=_TokenDoContexto()
    ) as cliente:
        resposta = await _segurar(cliente, c, c.painel)
    await app.state.engine.dispose()
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "indisponivel"


async def test_hold_vencido_do_mesmo_dentista_e_expirado_ao_segurar(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    antigo = await _hold(cliente, c, c.painel)
    await _vencer(fabrica, antigo)
    assert (await _segurar(cliente, c, c.painel)).status_code == 201
    assert (await _ler(fabrica, antigo)).status == "expirado"


async def test_hold_vencido_de_outro_dentista_no_consultorio_e_expirado_ao_segurar(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    async with fabrica() as sessao:
        vencido = Agendamento(
            clinica_id=c.clinica_id,
            dentista_id=c.dentista_b,
            consultorio_id=c.consultorio_1,
            procedimento_id=c.procedimento_id,
            periodo=Range(c.h(9), c.h(9, 30)),
            status="segurado",
            expira_em=func.now() - timedelta(seconds=1),
            origem="painel",
        )
        sessao.add(vencido)
        await sessao.commit()
    assert (await _segurar(cliente, c, c.painel)).status_code == 201
    assert (await _ler(fabrica, str(vencido.id))).status == "expirado"


async def test_segurar_exige_chave_de_idempotencia(cliente: httpx.AsyncClient, c: Cenario) -> None:
    resposta = await cliente.post(
        "/holds",
        headers=c.painel,
        json={
            "procedimento_id": str(c.procedimento_id),
            "dentista_id": str(c.dentista_a),
            "inicio": c.h(9).isoformat(),
        },
    )
    assert resposta.status_code == 422


@pytest.mark.parametrize("campo", ["clinica_id", "telefone", "paciente_id", "chamada_id"])
async def test_identidade_nunca_vem_no_corpo(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica, campo: str
) -> None:
    resposta = await cliente.post(
        "/holds",
        headers={**await _chamada(fabrica, c), **_chave()},
        json={
            "procedimento_id": str(c.procedimento_id),
            "dentista_id": str(c.dentista_a),
            "inicio": c.h(9).isoformat(),
            campo: str(uuid4()),
        },
    )
    assert resposta.status_code == 422


async def test_segurar_recusa_data_sem_fuso(cliente: httpx.AsyncClient, c: Cenario) -> None:
    resposta = await cliente.post(
        "/holds",
        headers={**c.painel, **_chave()},
        json={
            "procedimento_id": str(c.procedimento_id),
            "dentista_id": str(c.dentista_a),
            "inicio": "2030-01-07T09:00:00",
        },
    )
    assert resposta.status_code == 422


# ---- Publicação na fila de atraso


async def test_hold_novo_publica_uma_vez_com_a_duracao(
    cliente: httpx.AsyncClient, c: Cenario, publicador: PublicadorFalso
) -> None:
    hold_id = await _hold(cliente, c, c.painel)
    assert publicador.holds == [(UUID(hold_id), timedelta(minutes=3))]


async def test_duracao_publicada_vem_da_configuracao(banco: str, c: Cenario) -> None:
    publicador = PublicadorFalso()
    app = criar_app(
        ConfigPostgres(db=banco),
        ConfigAgenda(antecedencia_minima_min=0, duracao_hold_min=5),
        publicador,
    )
    transporte = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transporte, base_url="http://api", auth=_TokenDoContexto()
    ) as cliente:
        await _hold(cliente, c, c.painel)
    await app.state.engine.dispose()
    assert [duracao for _, duracao in publicador.holds] == [timedelta(minutes=5)]


async def test_retry_com_resposta_guardada_nao_publica_de_novo(
    cliente: httpx.AsyncClient, c: Cenario, publicador: PublicadorFalso
) -> None:
    await _segurar(cliente, c, c.painel, chave="k")
    await _segurar(cliente, c, c.painel, chave="k")
    assert len(publicador.holds) == 1


async def test_retry_de_hold_vencido_publica_o_hold_novo(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica, publicador: PublicadorFalso
) -> None:
    primeira = await _segurar(cliente, c, c.painel, chave="k")
    await _vencer(fabrica, primeira.json()["hold_id"])
    segunda = await _segurar(cliente, c, c.painel, chave="k")
    assert [str(h) for h, _ in publicador.holds] == [
        primeira.json()["hold_id"],
        segunda.json()["hold_id"],
    ]


@pytest.mark.parametrize("inicio", [(9, 7), (9, 0)])
async def test_falha_ao_segurar_nao_publica(
    cliente: httpx.AsyncClient,
    c: Cenario,
    publicador: PublicadorFalso,
    inicio: tuple[int, int],
) -> None:
    await _hold(cliente, c, c.painel)
    publicador.holds.clear()
    resposta = await _segurar(cliente, c, c.painel, c.h(*inicio))
    assert resposta.status_code == 409
    assert publicador.holds == []


async def test_mensagem_so_e_publicada_depois_do_commit(
    banco: str, c: Cenario, fabrica: Fabrica
) -> None:
    class Observador(PublicadorFalso):
        status: str | None = None

        async def hold_criado(self, hold_id: UUID, duracao: timedelta) -> None:
            self.status = (await _ler(fabrica, str(hold_id))).status

    observador = Observador()
    app = criar_app(ConfigPostgres(db=banco), ConfigAgenda(antecedencia_minima_min=0), observador)
    transporte = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transporte, base_url="http://api", auth=_TokenDoContexto()
    ) as cliente:
        await _hold(cliente, c, c.painel)
    await app.state.engine.dispose()
    assert observador.status == "segurado"


# ---- Eventos agendamento.*


def _tipos(publicador: PublicadorFalso) -> list[tuple[str, str]]:
    return [(e.tipo, str(e.agendamento_id)) for e in publicador.eventos_publicados]


async def test_segurar_publica_criado(
    cliente: httpx.AsyncClient, c: Cenario, publicador: PublicadorFalso
) -> None:
    hold_id = await _hold(cliente, c, c.painel)
    assert _tipos(publicador) == [("criado", hold_id)]
    evento = publicador.eventos_publicados[0]
    assert evento.routing_key == "agendamento.criado"
    assert evento.status == "segurado"
    assert evento.clinica_id == c.clinica_id
    assert evento.dentista_id == c.dentista_a
    assert evento.consultorio_id == c.consultorio_1
    assert evento.inicio == c.h(9)
    assert evento.fim_periodo == c.h(9, 30)
    assert evento.anterior is None


async def test_confirmar_publica_confirmado(
    cliente: httpx.AsyncClient, c: Cenario, publicador: PublicadorFalso
) -> None:
    hold_id = await _consulta(cliente, c)
    assert _tipos(publicador) == [("criado", hold_id), ("confirmado", hold_id)]
    assert publicador.eventos_publicados[-1].status == "confirmado"


async def test_remarcar_publica_um_evento_com_os_dois_horarios(
    cliente: httpx.AsyncClient, c: Cenario, publicador: PublicadorFalso
) -> None:
    consulta = await _consulta(cliente, c)
    novo = await _hold(cliente, c, c.painel, c.h(10))
    publicador.eventos_publicados.clear()
    resposta = await _remarcar(cliente, consulta, novo, c.painel)
    assert resposta.status_code == 200, resposta.text
    assert _tipos(publicador) == [("remarcado", novo)]
    evento = publicador.eventos_publicados[0]
    assert evento.routing_key == "agendamento.remarcado"
    assert (evento.status, evento.inicio) == ("confirmado", c.h(10))
    assert evento.anterior is not None
    assert str(evento.anterior.agendamento_id) == consulta
    assert (evento.anterior.status, evento.anterior.inicio) == ("cancelado", c.h(9))


async def test_cancelar_publica_cancelado(
    cliente: httpx.AsyncClient, c: Cenario, publicador: PublicadorFalso
) -> None:
    consulta = await _consulta(cliente, c)
    publicador.eventos_publicados.clear()
    resposta = await _cancelar(cliente, consulta, c.painel)
    assert resposta.status_code == 200, resposta.text
    assert _tipos(publicador) == [("cancelado", consulta)]
    assert publicador.eventos_publicados[0].status == "cancelado"


async def test_evento_nao_leva_dados_do_paciente(
    cliente: httpx.AsyncClient, c: Cenario, publicador: PublicadorFalso
) -> None:
    consulta = await _consulta(cliente, c)
    await _cancelar(cliente, consulta, c.painel, "dor de dente")
    for evento in publicador.eventos_publicados:
        corpo = evento.model_dump_json()
        assert str(c.paciente_id) not in corpo
        assert "dor de dente" not in corpo
    assert set(publicador.eventos_publicados[0].model_dump()) == {
        "tipo",
        "agendamento_id",
        "clinica_id",
        "dentista_id",
        "consultorio_id",
        "status",
        "inicio",
        "fim_periodo",
        "anterior",
    }


async def test_retry_nao_publica_de_novo(
    cliente: httpx.AsyncClient, c: Cenario, publicador: PublicadorFalso
) -> None:
    hold_id = (await _segurar(cliente, c, c.painel, chave="s")).json()["hold_id"]
    await _segurar(cliente, c, c.painel, chave="s")
    await _confirmar(cliente, hold_id, c.painel, c.paciente_id, chave="c")
    await _confirmar(cliente, hold_id, c.painel, c.paciente_id, chave="c")
    novo = await _hold(cliente, c, c.painel, c.h(10))
    await _remarcar(cliente, hold_id, novo, c.painel, chave="r")
    await _remarcar(cliente, hold_id, novo, c.painel, chave="r")
    await _cancelar(cliente, novo, c.painel, chave="x")
    await _cancelar(cliente, novo, c.painel, chave="x")
    assert [tipo for tipo, _ in _tipos(publicador)] == [
        "criado",
        "confirmado",
        "criado",
        "remarcado",
        "cancelado",
    ]


async def test_operacao_recusada_nao_publica(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica, publicador: PublicadorFalso
) -> None:
    hold_id = await _hold(cliente, c, c.painel)
    await _vencer(fabrica, hold_id)
    publicador.eventos_publicados.clear()
    assert (await _confirmar(cliente, hold_id, c.painel, c.paciente_id)).status_code == 409
    assert (await _cancelar(cliente, hold_id, c.painel)).status_code == 409
    assert publicador.eventos_publicados == []


async def test_segurar_ocupado_nao_publica(
    cliente: httpx.AsyncClient, c: Cenario, publicador: PublicadorFalso
) -> None:
    await _hold(cliente, c, c.painel)
    publicador.eventos_publicados.clear()
    assert (await _segurar(cliente, c, c.painel)).status_code == 409
    assert publicador.eventos_publicados == []


async def test_limpeza_ao_segurar_publica_expirado_antes_do_criado(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica, publicador: PublicadorFalso
) -> None:
    vencido = await _hold(cliente, c, c.painel)
    await _vencer(fabrica, vencido)
    publicador.eventos_publicados.clear()
    novo = await _hold(cliente, c, c.painel)
    assert _tipos(publicador) == [("expirado", vencido), ("criado", novo)]
    evento = publicador.eventos_publicados[0]
    assert (evento.routing_key, evento.status) == ("agendamento.expirado", "expirado")
    assert evento.inicio == c.h(9)


async def test_limpeza_vale_para_hold_de_outro_dentista_no_consultorio(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica, publicador: PublicadorFalso
) -> None:
    async with fabrica() as sessao:
        await sessao.execute(
            update(GradeTrabalho)
            .where(GradeTrabalho.clinica_id == c.clinica_id)
            .values(consultorio_id=c.consultorio_1)
        )
        await sessao.commit()
    vencido = (await _segurar(cliente, c, c.painel, dentista=c.dentista_b)).json()["hold_id"]
    await _vencer(fabrica, vencido)
    publicador.eventos_publicados.clear()
    novo = await _hold(cliente, c, c.painel)
    assert _tipos(publicador) == [("expirado", vencido), ("criado", novo)]


async def test_limpeza_e_publicada_mesmo_quando_o_novo_hold_da_ocupado(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica, publicador: PublicadorFalso
) -> None:
    await _hold(cliente, c, c.painel)
    vencido = await _hold(cliente, c, c.painel, c.h(10))
    await _vencer(fabrica, vencido)
    publicador.eventos_publicados.clear()
    resposta = await _segurar(cliente, c, c.painel)
    assert resposta.json()["motivo"] == "ocupado"
    assert _tipos(publicador) == [("expirado", vencido)]
    assert (await _ler(fabrica, vencido)).status == "expirado"


async def test_retry_de_hold_vencido_publica_expirado_e_criado(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica, publicador: PublicadorFalso
) -> None:
    primeiro = (await _segurar(cliente, c, c.painel, chave="k")).json()["hold_id"]
    await _vencer(fabrica, primeiro)
    publicador.eventos_publicados.clear()
    segundo = (await _segurar(cliente, c, c.painel, chave="k")).json()["hold_id"]
    assert _tipos(publicador) == [("expirado", primeiro), ("criado", segundo)]


async def test_transacao_desfeita_nao_publica_a_limpeza(
    cliente: httpx.AsyncClient,
    c: Cenario,
    fabrica: Fabrica,
    publicador: PublicadorFalso,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vencido = await _hold(cliente, c, c.painel)
    await _vencer(fabrica, vencido)
    publicador.eventos_publicados.clear()

    async def falha(*_: Any, **__: Any) -> None:
        raise RuntimeError("falha ao gravar")

    monkeypatch.setattr("clinica_api.agenda.idempotencia.guardar", falha)
    with pytest.raises(RuntimeError):
        await _segurar(cliente, c, c.painel)
    assert publicador.eventos_publicados == []
    assert (await _ler(fabrica, vencido)).status == "segurado"


async def test_eventos_so_saem_depois_do_commit(banco: str, c: Cenario, fabrica: Fabrica) -> None:
    class Observador(PublicadorFalso):
        vistos: list[str]

        def __init__(self) -> None:
            super().__init__()
            self.vistos = []

        async def eventos(self, eventos: Sequence[EventoAgendamento]) -> None:
            for evento in eventos:
                self.vistos.append((await _ler(fabrica, str(evento.agendamento_id))).status)

    observador = Observador()
    app = criar_app(ConfigPostgres(db=banco), ConfigAgenda(antecedencia_minima_min=0), observador)
    transporte = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transporte, base_url="http://api", auth=_TokenDoContexto()
    ) as cliente:
        consulta = await _consulta(cliente, c)
        novo = await _hold(cliente, c, c.painel, c.h(10))
        await _remarcar(cliente, consulta, novo, c.painel)
        await _cancelar(cliente, novo, c.painel)
    await app.state.engine.dispose()
    assert observador.vistos == ["segurado", "confirmado", "segurado", "confirmado", "cancelado"]


async def test_hold_e_evento_sao_publicados_juntos(
    banco: str, c: Cenario, fabrica: Fabrica
) -> None:
    class Dependentes(PublicadorFalso):
        """Em sequência, os dois limites de publicação somariam; aqui, um espera o outro."""

        def __init__(self) -> None:
            super().__init__()
            self.evento_iniciado = asyncio.Event()

        async def hold_criado(self, hold_id: UUID, duracao: timedelta) -> None:
            await self.evento_iniciado.wait()

        async def eventos(self, eventos: Sequence[EventoAgendamento]) -> None:
            self.evento_iniciado.set()

    app = criar_app(
        ConfigPostgres(db=banco), ConfigAgenda(antecedencia_minima_min=0), Dependentes()
    )
    transporte = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transporte, base_url="http://api", auth=_TokenDoContexto()
    ) as cliente:
        async with asyncio.timeout(5):
            await _hold(cliente, c, c.painel)
    await app.state.engine.dispose()


async def test_evento_perdido_nao_derruba_a_operacao(
    banco: str, c: Cenario, fabrica: Fabrica, caplog: pytest.LogCaptureFixture
) -> None:
    # Sem abrir(): o publicador real sem conexão com o broker.
    app = criar_app(
        ConfigPostgres(db=banco),
        ConfigAgenda(antecedencia_minima_min=0),
        PublicadorRabbit(ConfigRabbit()),
    )
    transporte = httpx.ASGITransport(app=app)
    with caplog.at_level("WARNING"):
        async with httpx.AsyncClient(
            transport=transporte, base_url="http://api", auth=_TokenDoContexto()
        ) as cliente:
            consulta = await _consulta(cliente, c)
            assert (await _cancelar(cliente, consulta, c.painel)).status_code == 200
    await app.state.engine.dispose()
    assert "eventos não publicados" in caplog.text
    assert (await _ler(fabrica, consulta)).status == "cancelado"


async def test_painel_recebe_os_eventos_pelo_broker(
    banco: str, vhost: str, c: Cenario, fabrica: Fabrica
) -> None:
    publicador = PublicadorRabbit(ConfigRabbit())
    await publicador.abrir()
    async with asyncio.timeout(10):
        while True:
            if publicador._canal is not None:
                break
            await asyncio.sleep(0.02)
    conexao = await conectar(ConfigRabbit())
    fila = await declarar_fila_painel(await abrir_canal(conexao))
    app = criar_app(ConfigPostgres(db=banco), ConfigAgenda(antecedencia_minima_min=0), publicador)
    transporte = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transporte, base_url="http://api", auth=_TokenDoContexto()
        ) as cliente:
            consulta = await _consulta(cliente, c)
            novo = await _hold(cliente, c, c.painel, c.h(10))
            await _remarcar(cliente, consulta, novo, c.painel)
            await _cancelar(cliente, novo, c.painel)
            vencido = await _hold(cliente, c, c.painel, c.h(11))
            await _vencer(fabrica, vencido)
            novo_hold = await _hold(cliente, c, c.painel, c.h(11))

        recebidos: list[tuple[str | None, str]] = []
        async with asyncio.timeout(10):
            while len(recebidos) < 8:
                mensagem = await fila.get(fail=False)
                if mensagem is None:
                    await asyncio.sleep(0.02)
                    continue
                await mensagem.ack()
                evento = EventoAgendamento.model_validate_json(mensagem.body)
                recebidos.append((mensagem.routing_key, str(evento.agendamento_id)))
        assert recebidos == [
            ("agendamento.criado", consulta),
            ("agendamento.confirmado", consulta),
            ("agendamento.criado", novo),
            ("agendamento.remarcado", novo),
            ("agendamento.cancelado", novo),
            ("agendamento.criado", vencido),
            ("agendamento.expirado", vencido),
            ("agendamento.criado", novo_hold),
        ]
    finally:
        await conexao.close()
        await publicador.fechar()
        await app.state.engine.dispose()


# ---- Confirmar: quem pode confirmar o quê


async def test_ligacao_confirma_o_proprio_hold(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    cabecalhos = await _chamada(fabrica, c, c.paciente_id)
    hold_id = await _hold(cliente, c, cabecalhos)
    resposta = await _confirmar(cliente, hold_id, cabecalhos)
    assert resposta.status_code == 200
    assert resposta.json()["paciente_id"] == str(c.paciente_id)
    agendamento = await _ler(fabrica, hold_id)
    assert agendamento.status == "confirmado"
    assert agendamento.paciente_id == c.paciente_id
    assert agendamento.expira_em is None


async def test_ligacao_nao_confirma_hold_de_outra_chamada(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    hold_id = await _hold(cliente, c, await _chamada(fabrica, c))
    resposta = await _confirmar(cliente, hold_id, await _chamada(fabrica, c, c.paciente_id))
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "hold_perdido"
    assert (await _ler(fabrica, hold_id)).status == "segurado"


async def test_ligacao_nao_confirma_hold_do_painel(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    hold_id = await _hold(cliente, c, c.painel)
    resposta = await _confirmar(cliente, hold_id, await _chamada(fabrica, c, c.paciente_id))
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "hold_perdido"


async def test_painel_confirma_hold_do_painel(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    hold_id = await _hold(cliente, c, c.painel)
    resposta = await _confirmar(cliente, hold_id, c.painel, c.paciente_id)
    assert resposta.status_code == 200
    assert (await _ler(fabrica, hold_id)).status == "confirmado"


async def test_painel_nao_confirma_hold_de_ligacao(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    hold_id = await _hold(cliente, c, await _chamada(fabrica, c))
    resposta = await _confirmar(cliente, hold_id, c.painel, c.paciente_id)
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "hold_perdido"
    assert (await _ler(fabrica, hold_id)).status == "segurado"


async def test_ligacao_nao_confirma_hold_do_painel_com_a_chamada_dela(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    # O CHECK só vale num sentido: o banco aceita hold do painel com chamada_id.
    cabecalhos = await _chamada(fabrica, c, c.paciente_id)
    async with fabrica() as sessao:
        hold = Agendamento(
            clinica_id=c.clinica_id,
            dentista_id=c.dentista_a,
            consultorio_id=c.consultorio_1,
            procedimento_id=c.procedimento_id,
            periodo=Range(c.h(9), c.h(9, 30)),
            status="segurado",
            expira_em=func.now() + timedelta(minutes=3),
            chamada_id=UUID(cabecalhos["X-Chamada-Id"]),
            origem="painel",
        )
        sessao.add(hold)
        await sessao.commit()
    resposta = await _confirmar(cliente, str(hold.id), cabecalhos)
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "hold_perdido"


async def test_hold_vencido_nao_e_confirmado(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    hold_id = await _hold(cliente, c, c.painel)
    await _vencer(fabrica, hold_id)
    resposta = await _confirmar(cliente, hold_id, c.painel, c.paciente_id)
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "hold_perdido"


async def test_hold_inexistente_e_perdido(cliente: httpx.AsyncClient, c: Cenario) -> None:
    resposta = await _confirmar(cliente, str(uuid4()), c.painel, c.paciente_id)
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "hold_perdido"


async def test_hold_de_outra_clinica_e_perdido(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    hold_id = await _hold(cliente, c, c.painel)
    async with fabrica() as sessao:
        outra = Clinica(nome="O", telefone="+5521", endereco="R", politica_cancelamento_horas=24)
        sessao.add(outra)
        await sessao.flush()
        paciente = Paciente(
            clinica_id=outra.id,
            nome="João",
            telefone_e164="+5521999990000",
            data_nascimento=date(1980, 1, 1),
        )
        sessao.add(paciente)
        await sessao.commit()
    resposta = await _confirmar(cliente, hold_id, {"X-Clinica-Id": str(outra.id)}, paciente.id)
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "hold_perdido"


async def test_ligacao_nao_escolhe_paciente_no_corpo(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    cabecalhos = await _chamada(fabrica, c, c.paciente_id)
    hold_id = await _hold(cliente, c, cabecalhos)
    resposta = await _confirmar(cliente, hold_id, cabecalhos, c.paciente_id)
    assert resposta.status_code == 422
    assert resposta.json()["motivo"] == "paciente_nao_permitido"


async def test_ligacao_sem_paciente_selecionado(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    cabecalhos = await _chamada(fabrica, c)
    hold_id = await _hold(cliente, c, cabecalhos)
    resposta = await _confirmar(cliente, hold_id, cabecalhos)
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "paciente_nao_selecionado"


async def test_painel_exige_paciente(cliente: httpx.AsyncClient, c: Cenario) -> None:
    hold_id = await _hold(cliente, c, c.painel)
    resposta = await _confirmar(cliente, hold_id, c.painel)
    assert resposta.status_code == 422
    assert resposta.json()["motivo"] == "paciente_obrigatorio"


async def test_painel_recusa_paciente_inexistente(cliente: httpx.AsyncClient, c: Cenario) -> None:
    hold_id = await _hold(cliente, c, c.painel)
    resposta = await _confirmar(cliente, hold_id, c.painel, uuid4())
    assert resposta.status_code == 404
    assert resposta.json()["motivo"] == "paciente_nao_encontrado"


# ---- Remarcar


async def _remarcar(
    cliente: httpx.AsyncClient,
    agendamento_id: str,
    hold_id: str,
    cabecalhos: dict[str, str],
    chave: str | None = None,
) -> httpx.Response:
    return await cliente.post(
        f"/agendamentos/{agendamento_id}/remarcar",
        headers={**cabecalhos, "Idempotency-Key": chave or uuid4().hex},
        json={"hold_id": hold_id},
    )


async def test_remarcar_confirma_o_novo_e_cancela_o_antigo(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    antigo = await _consulta(cliente, c)
    cabecalhos = await _chamada(fabrica, c, c.paciente_id, verificada=True)
    novo = await _hold(cliente, c, cabecalhos, c.h(10))
    resposta = await _remarcar(cliente, antigo, novo, cabecalhos)
    assert resposta.status_code == 200
    assert resposta.json()["agendamento_id"] == novo
    assert resposta.json()["paciente_id"] == str(c.paciente_id)
    agendamento_antigo = await _ler(fabrica, antigo)
    assert agendamento_antigo.status == "cancelado"
    assert agendamento_antigo.motivo_cancelamento == "remarcado"
    agendamento_novo = await _ler(fabrica, novo)
    assert agendamento_novo.status == "confirmado"
    assert agendamento_novo.paciente_id == c.paciente_id
    assert c.h(9) in await _inicios_livres(cliente, c)


async def test_remarcar_com_hold_perdido_mantem_a_consulta(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    antigo = await _consulta(cliente, c)
    cabecalhos = await _chamada(fabrica, c, c.paciente_id, verificada=True)
    novo = await _hold(cliente, c, cabecalhos, c.h(10))
    await _vencer(fabrica, novo)
    resposta = await _remarcar(cliente, antigo, novo, cabecalhos)
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "hold_perdido"
    assert (await _ler(fabrica, antigo)).status == "confirmado"


async def test_remarcar_consulta_ja_cancelada_mantem_o_hold(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    antigo = await _consulta(cliente, c)
    novo = await _hold(cliente, c, c.painel, c.h(10))
    await _cancelar(cliente, antigo, c.painel)
    resposta = await _remarcar(cliente, antigo, novo, c.painel)
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "agendamento_nao_ativo"
    assert (await _ler(fabrica, novo)).status == "segurado"


async def test_remarcar_exige_identidade_verificada(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    antigo = await _consulta(cliente, c)
    cabecalhos = await _chamada(fabrica, c, c.paciente_id, verificada=False)
    novo = await _hold(cliente, c, cabecalhos, c.h(10))
    resposta = await _remarcar(cliente, antigo, novo, cabecalhos)
    assert resposta.status_code == 403
    assert resposta.json()["motivo"] == "identidade_nao_verificada"
    assert (await _ler(fabrica, antigo)).status == "confirmado"


async def test_remarcar_consulta_de_outro_paciente_responde_inexistente(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    antigo = await _consulta(cliente, c)
    async with fabrica() as sessao:
        outro = Paciente(
            clinica_id=c.clinica_id,
            nome="Filho",
            telefone_e164="+5511999990000",
            data_nascimento=date(2015, 1, 1),
        )
        sessao.add(outro)
        await sessao.commit()
    cabecalhos = await _chamada(fabrica, c, outro.id, verificada=True)
    novo = await _hold(cliente, c, cabecalhos, c.h(10))
    resposta = await _remarcar(cliente, antigo, novo, cabecalhos)
    assert resposta.status_code == 404
    assert resposta.json()["motivo"] == "agendamento_nao_encontrado"
    assert (await _ler(fabrica, antigo)).status == "confirmado"


async def test_remarcar_para_horario_sobreposto_e_limitacao_conhecida(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    await _consulta(cliente, c)
    cabecalhos = await _chamada(fabrica, c, c.paciente_id, verificada=True)
    resposta = await _segurar(cliente, c, cabecalhos, c.h(9, 15))
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "ocupado"


# ---- Cancelar


async def _cancelar(
    cliente: httpx.AsyncClient,
    agendamento_id: str,
    cabecalhos: dict[str, str],
    motivo: str | None = None,
    chave: str | None = None,
) -> httpx.Response:
    return await cliente.post(
        f"/agendamentos/{agendamento_id}/cancelar",
        headers={**cabecalhos, "Idempotency-Key": chave or uuid4().hex},
        json={} if motivo is None else {"motivo": motivo},
    )


async def test_cancelar_libera_o_horario_e_guarda_o_motivo(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    agendamento_id = await _consulta(cliente, c)
    assert c.h(9) not in await _inicios_livres(cliente, c)
    cabecalhos = await _chamada(fabrica, c, c.paciente_id, verificada=True)
    resposta = await _cancelar(cliente, agendamento_id, cabecalhos, "Viagem")
    assert resposta.status_code == 200
    assert resposta.json()["dentro_do_prazo"] is True
    assert resposta.json()["politica_cancelamento_horas"] == 24
    agendamento = await _ler(fabrica, agendamento_id)
    assert agendamento.status == "cancelado"
    assert agendamento.motivo_cancelamento == "Viagem"
    assert c.h(9) in await _inicios_livres(cliente, c)


async def _consulta_daqui_a(fabrica: Fabrica, c: Cenario, deslocamento: timedelta) -> str:
    async with fabrica() as sessao:
        agora = (await sessao.execute(select(func.now()))).scalar_one()
        agendamento = Agendamento(
            clinica_id=c.clinica_id,
            dentista_id=c.dentista_b,
            consultorio_id=c.consultorio_2,
            procedimento_id=c.procedimento_id,
            paciente_id=c.paciente_id,
            periodo=Range(agora + deslocamento, agora + deslocamento + timedelta(minutes=30)),
            status="confirmado",
            origem="painel",
        )
        sessao.add(agendamento)
        await sessao.commit()
        return str(agendamento.id)


async def test_cancelar_fora_do_prazo_e_permitido_e_avisa(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    agendamento_id = await _consulta_daqui_a(fabrica, c, timedelta(hours=2))
    resposta = await _cancelar(cliente, agendamento_id, c.painel)
    assert resposta.status_code == 200
    assert resposta.json()["dentro_do_prazo"] is False
    assert (await _ler(fabrica, agendamento_id)).status == "cancelado"


async def test_cancelar_consulta_passada_e_recusado(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    agendamento_id = await _consulta_daqui_a(fabrica, c, timedelta(hours=-2))
    resposta = await _cancelar(cliente, agendamento_id, c.painel)
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "agendamento_passado"
    assert (await _ler(fabrica, agendamento_id)).status == "confirmado"


async def test_remarcar_consulta_passada_e_recusado(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    agendamento_id = await _consulta_daqui_a(fabrica, c, timedelta(hours=-2))
    novo = await _hold(cliente, c, c.painel, c.h(10))
    resposta = await _remarcar(cliente, agendamento_id, novo, c.painel)
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "agendamento_passado"


async def test_cancelar_exige_identidade_verificada(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    agendamento_id = await _consulta(cliente, c)
    cabecalhos = await _chamada(fabrica, c, c.paciente_id, verificada=False)
    resposta = await _cancelar(cliente, agendamento_id, cabecalhos)
    assert resposta.status_code == 403
    assert resposta.json()["motivo"] == "identidade_nao_verificada"


async def test_cancelar_consulta_de_outro_paciente_responde_inexistente(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    agendamento_id = await _consulta(cliente, c)
    async with fabrica() as sessao:
        outro = Paciente(
            clinica_id=c.clinica_id,
            nome="Filho",
            telefone_e164="+5511999990000",
            data_nascimento=date(2015, 1, 1),
        )
        sessao.add(outro)
        await sessao.commit()
    cabecalhos = await _chamada(fabrica, c, outro.id, verificada=True)
    resposta = await _cancelar(cliente, agendamento_id, cabecalhos)
    assert resposta.status_code == 404


async def test_hold_nao_e_cancelado_por_esta_rota(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    hold_id = await _hold(cliente, c, c.painel)
    resposta = await _cancelar(cliente, hold_id, c.painel)
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "agendamento_nao_ativo"


# ---- Idempotência


async def test_retry_de_segurar_devolve_o_mesmo_hold(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    cabecalhos = await _chamada(fabrica, c)
    primeira = await _segurar(cliente, c, cabecalhos, chave="k")
    segunda = await _segurar(cliente, c, cabecalhos, chave="k")
    assert segunda.status_code == 201
    assert segunda.json() == primeira.json()


async def test_retry_simultaneo_nao_duplica(cliente: httpx.AsyncClient, c: Cenario) -> None:
    respostas = await asyncio.gather(*(_segurar(cliente, c, c.painel, chave="k") for _ in range(5)))
    assert {r.status_code for r in respostas} == {201}
    assert len({r.json()["hold_id"] for r in respostas}) == 1


async def test_ocupado_nao_fica_guardado(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    outro = await _hold(cliente, c, c.painel)
    assert (await _segurar(cliente, c, c.painel, chave="k")).status_code == 409
    await _vencer(fabrica, outro)
    assert (await _segurar(cliente, c, c.painel, chave="k")).status_code == 201


async def test_hold_vencido_nao_e_devolvido_num_retry(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    primeira = await _segurar(cliente, c, c.painel, chave="k")
    await _vencer(fabrica, primeira.json()["hold_id"])
    segunda = await _segurar(cliente, c, c.painel, chave="k")
    assert segunda.status_code == 201
    assert segunda.json()["hold_id"] != primeira.json()["hold_id"]
    assert (await _ler(fabrica, segunda.json()["hold_id"])).status == "segurado"
    terceira = await _segurar(cliente, c, c.painel, chave="k")
    assert terceira.json()["hold_id"] == segunda.json()["hold_id"]


async def test_retry_de_confirmar_devolve_a_mesma_resposta(
    cliente: httpx.AsyncClient, c: Cenario
) -> None:
    hold_id = await _hold(cliente, c, c.painel)
    primeira = await _confirmar(cliente, hold_id, c.painel, c.paciente_id, chave="k")
    segunda = await _confirmar(cliente, hold_id, c.painel, c.paciente_id, chave="k")
    assert segunda.status_code == 200
    assert segunda.json() == primeira.json()


async def test_hold_perdido_nao_fica_guardado(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    hold_id = await _hold(cliente, c, c.painel)
    falha = await _confirmar(cliente, hold_id, c.painel, chave="k")
    assert falha.status_code == 422
    sucesso = await _confirmar(cliente, hold_id, c.painel, c.paciente_id, chave="k")
    assert sucesso.status_code == 200


async def test_retry_de_remarcar_devolve_a_mesma_resposta(
    cliente: httpx.AsyncClient, c: Cenario
) -> None:
    antigo = await _consulta(cliente, c)
    novo = await _hold(cliente, c, c.painel, c.h(10))
    primeira = await _remarcar(cliente, antigo, novo, c.painel, chave="k")
    segunda = await _remarcar(cliente, antigo, novo, c.painel, chave="k")
    assert segunda.status_code == 200
    assert segunda.json() == primeira.json()


async def test_retry_de_cancelar_devolve_a_mesma_resposta(
    cliente: httpx.AsyncClient, c: Cenario
) -> None:
    agendamento_id = await _consulta(cliente, c)
    primeira = await _cancelar(cliente, agendamento_id, c.painel, chave="k")
    segunda = await _cancelar(cliente, agendamento_id, c.painel, chave="k")
    assert segunda.status_code == 200
    assert segunda.json() == primeira.json()


async def test_resposta_com_mais_de_24_horas_nao_e_devolvida(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    agendamento_id = await _consulta(cliente, c)
    assert (await _cancelar(cliente, agendamento_id, c.painel, chave="k")).status_code == 200
    async with fabrica() as sessao:
        await sessao.execute(
            update(Idempotencia)
            .where(Idempotencia.chave.like(f"{c.clinica_id}:cancelar:k"))
            .values(criado_em=func.now() - timedelta(hours=24, seconds=1))
        )
        await sessao.commit()
    resposta = await _cancelar(cliente, agendamento_id, c.painel, chave="k")
    assert resposta.status_code == 409
    assert resposta.json()["motivo"] == "agendamento_nao_ativo"


async def test_mesma_chave_em_operacoes_diferentes_nao_se_mistura(
    cliente: httpx.AsyncClient, c: Cenario
) -> None:
    resposta = await _segurar(cliente, c, c.painel, chave="k")
    confirmacao = await _confirmar(
        cliente, resposta.json()["hold_id"], c.painel, c.paciente_id, chave="k"
    )
    assert confirmacao.status_code == 200
    assert "agendamento_id" in confirmacao.json()


async def test_chave_e_separada_por_clinica(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    async with fabrica() as sessao:
        outra = Clinica(nome="O", telefone="+5521", endereco="R", politica_cancelamento_horas=24)
        sessao.add(outra)
        await sessao.commit()
    assert (await _segurar(cliente, c, c.painel, chave="k")).status_code == 201
    resposta = await _segurar(cliente, c, {"X-Clinica-Id": str(outra.id)}, chave="k")
    assert resposta.status_code == 404
    assert resposta.json()["motivo"] == "procedimento_nao_encontrado"


# ---- Concorrência


def _motivos(respostas: list[httpx.Response]) -> Counter[str]:
    return Counter(
        "ok" if r.status_code == 201 else r.json().get("motivo", str(r.status_code))
        for r in respostas
    )


async def _em_paralelo(*tarefas: Any) -> list[httpx.Response]:
    return list(await asyncio.gather(*tarefas))


def _conferir_alternativas(falhas: list[dict[str, Any]], inicio: datetime, fim: datetime) -> None:
    for falha in falhas:
        assert falha["alternativas"]
        for alternativa in falha["alternativas"]:
            # Compara instantes: a API devolve os horários em UTC.
            assert datetime.fromisoformat(alternativa["fim_periodo"]) <= inicio or (
                datetime.fromisoformat(alternativa["inicio"]) >= fim
            )


async def test_50_ligacoes_no_mesmo_horario(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    chamadas = [await _chamada(fabrica, c) for _ in range(50)]
    respostas = await _em_paralelo(*(_segurar(cliente, c, cab) for cab in chamadas))
    assert _motivos(respostas) == {"ok": 1, "ocupado": 49}
    perdedoras = [r.json() for r in respostas if r.status_code == 409]
    _conferir_alternativas(perdedoras, c.h(9), c.h(9, 30))


async def test_painel_concorre_com_ligacoes_sob_a_mesma_regra(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    cabecalhos: list[dict[str, str]] = [await _chamada(fabrica, c) for _ in range(9)]
    respostas = await _em_paralelo(*(_segurar(cliente, c, cab) for cab in [*cabecalhos, c.painel]))
    assert _motivos(respostas) == {"ok": 1, "ocupado": 9}


async def test_consultorio_compartilhado_entre_dentistas(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica
) -> None:
    async with fabrica() as sessao:
        grade = await sessao.scalar(
            select(GradeTrabalho).where(GradeTrabalho.dentista_id == c.dentista_b)
        )
        assert grade is not None
        grade.consultorio_id = c.consultorio_1
        await sessao.commit()
    tarefas: list[Any] = []
    for _ in range(25):
        tarefas.append(_segurar(cliente, c, c.painel, dentista=c.dentista_a))
        tarefas.append(_segurar(cliente, c, c.painel, dentista=c.dentista_b))
    assert _motivos(await _em_paralelo(*tarefas)) == {"ok": 1, "ocupado": 49}


async def test_confirmar_enquanto_outros_tentam_segurar(
    cliente: httpx.AsyncClient, c: Cenario
) -> None:
    hold_id = await _hold(cliente, c, c.painel)
    confirmacao, *tentativas = await _em_paralelo(
        _confirmar(cliente, hold_id, c.painel, c.paciente_id),
        *(_segurar(cliente, c, c.painel) for _ in range(20)),
    )
    assert confirmacao.status_code == 200
    assert _motivos(tentativas) == {"ocupado": 20}


async def _conferir_rodada(
    respostas: list[httpx.Response],
    c: Cenario,
    fabrica: Fabrica,
    publicador: PublicadorFalso,
) -> None:
    assert _motivos(respostas) == {"ok": 1, "ocupado": 49}
    vencedor = next(r.json() for r in respostas if r.status_code == 201)
    hold_id = UUID(vencedor["hold_id"])
    assert [hold for hold, _ in publicador.holds] == [hold_id]
    assert [(e.tipo, e.agendamento_id) for e in publicador.eventos_publicados] == [
        ("criado", hold_id)
    ]
    _conferir_alternativas(
        [r.json() for r in respostas if r.status_code == 409], c.h(9), c.h(9, 30)
    )
    async with fabrica() as sessao:
        linhas = await sessao.execute(
            select(Agendamento.id, Agendamento.status).where(Agendamento.clinica_id == c.clinica_id)
        )
        assert [tuple(linha) for linha in linhas] == [(hold_id, "segurado")]


async def test_t1_5_50_tarefas_no_mesmo_horario_100_vezes(
    banco: str,
    app: FastAPI,
    cliente: httpx.AsyncClient,
    c: Cenario,
    fabrica: Fabrica,
    publicador: PublicadorFalso,
) -> None:
    chamadas = [await _chamada(fabrica, c) for _ in range(50)]
    # O pool padrão limita a disputa a 15 conexões; aqui são 50 de verdade.
    engine = create_async_engine(ConfigPostgres(db=banco).url, pool_size=50)
    app.state.fabrica_sessoes = criar_fabrica_sessoes(engine)
    try:
        for rodada in range(1, 101):
            try:
                respostas = await _em_paralelo(*(_segurar(cliente, c, cab) for cab in chamadas))
                await _conferir_rodada(respostas, c, fabrica, publicador)
            except Exception as erro:
                erro.add_note(f"rodada {rodada} de 100")
                raise
            publicador.holds.clear()
            publicador.eventos_publicados.clear()
            async with fabrica() as sessao:
                await sessao.execute(
                    delete(Agendamento).where(Agendamento.clinica_id == c.clinica_id)
                )
                await sessao.commit()
    finally:
        await engine.dispose()


async def _espera_o_lock(
    fabrica: Fabrica, chave: UUID, operacao: Callable[[], Awaitable[httpx.Response]]
) -> httpx.Response:
    async with fabrica() as sessao:
        await travar(sessao, chave)
        tarefa = asyncio.ensure_future(operacao())
        await asyncio.sleep(0.3)
        assert not tarefa.done()
        await sessao.commit()
    return await asyncio.wait_for(tarefa, timeout=5)


@pytest.mark.parametrize("recurso", ["dentista_a", "consultorio_1"])
async def test_segurar_toma_o_lock_do_recurso(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica, recurso: str
) -> None:
    resposta = await _espera_o_lock(
        fabrica, getattr(c, recurso), lambda: _segurar(cliente, c, c.painel)
    )
    assert resposta.status_code == 201


@pytest.mark.parametrize("recurso", ["dentista_a", "consultorio_1"])
async def test_confirmar_toma_o_lock_do_recurso(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica, recurso: str
) -> None:
    hold_id = await _hold(cliente, c, c.painel)
    resposta = await _espera_o_lock(
        fabrica,
        getattr(c, recurso),
        lambda: _confirmar(cliente, hold_id, c.painel, c.paciente_id),
    )
    assert resposta.status_code == 200


@pytest.mark.parametrize("recurso", ["dentista_a", "consultorio_1", "dentista_b", "consultorio_2"])
async def test_remarcar_toma_o_lock_dos_dois_horarios(
    cliente: httpx.AsyncClient, c: Cenario, fabrica: Fabrica, recurso: str
) -> None:
    antigo = await _consulta(cliente, c)
    resposta_hold = await _segurar(cliente, c, c.painel, c.h(10), c.dentista_b)
    novo = resposta_hold.json()["hold_id"]
    resposta = await _espera_o_lock(
        fabrica, getattr(c, recurso), lambda: _remarcar(cliente, antigo, novo, c.painel)
    )
    assert resposta.status_code == 200
