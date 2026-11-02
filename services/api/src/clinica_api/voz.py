import re
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import date, datetime, time, timedelta
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_api import agenda
from clinica_api.agenda import Contexto, ErroAgenda
from clinica_api.esquemas import (
    CancelamentoPreparado,
    ConsultaFutura,
    ConsultasFuturas,
    HoldFalado,
    Opcao,
    OpcoesHorario,
    ProcedimentoResumo,
    Procedimentos,
    RemarcacaoPreparada,
    RemarcarEntrada,
    SegurarEntrada,
    SegurarPorNomeEntrada,
    Turno,
)
from clinica_api.horarios import listar_horarios_livres
from clinica_api.publicacao import PublicadorAgenda
from clinica_core.config import ConfigAgenda
from clinica_core.modelos import Agendamento, Clinica, Dentista, Procedimento

MAXIMO_OPCOES = 3
_DIAS_DA_SEMANA = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
_TITULOS = {"dr", "dra", "doutor", "doutora"}
_DISTANCIA_SUFICIENTE_S = 3600.0


class _Candidato(Protocol):
    @property
    def dentista_id(self) -> UUID: ...

    @property
    def inicio(self) -> datetime: ...


def normalizar(texto: str) -> str:
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn"
    )
    return " ".join(sem_acento.lower().split())


def turno_de(inicio: datetime, fuso: ZoneInfo) -> Turno:
    hora = inicio.astimezone(fuso).hour
    if hora < 12:
        return "manha"
    return "tarde" if hora < 18 else "noite"


def falar_horario(inicio: datetime, fuso: ZoneInfo) -> str:
    local = inicio.astimezone(fuso)
    minutos = f"{local.minute:02d}" if local.minute else ""
    return f"{_DIAS_DA_SEMANA[local.weekday()]}, dia {local.day}, às {local.hour}h{minutos}"


def escolher_variados[C: _Candidato](
    candidatos: Iterable[C], fuso: ZoneInfo, limite: int = MAXIMO_OPCOES
) -> list[C]:
    """O primeiro é o mais cedo; os outros variam dia, turno, dentista e, por fim, a distância."""
    restantes = sorted(candidatos, key=lambda c: c.inicio)
    escolhidos: list[C] = []

    def variedade(candidato: C) -> tuple[int, float]:
        local = candidato.inicio.astimezone(fuso)
        novidades = sum(
            (
                all(local.date() != e.inicio.astimezone(fuso).date() for e in escolhidos),
                all(
                    turno_de(candidato.inicio, fuso) != turno_de(e.inicio, fuso) for e in escolhidos
                ),
                all(candidato.dentista_id != e.dentista_id for e in escolhidos),
            )
        )
        distancias = (abs((candidato.inicio - e.inicio).total_seconds()) for e in escolhidos)
        return novidades, min([_DISTANCIA_SUFICIENTE_S, *distancias])

    while restantes and len(escolhidos) < limite:
        # max devolve o primeiro empate, que é o mais cedo.
        escolhido = max(restantes, key=variedade)
        escolhidos.append(escolhido)
        restantes.remove(escolhido)
    return sorted(escolhidos, key=lambda c: c.inicio)


async def _fuso(sessao: AsyncSession, clinica_id: UUID) -> ZoneInfo:
    nome = (await sessao.execute(select(Clinica.fuso).where(Clinica.id == clinica_id))).scalar_one()
    return ZoneInfo(nome)


async def resolver_procedimento(sessao: AsyncSession, clinica_id: UUID, nome: str) -> Procedimento:
    procurado = normalizar(nome)
    procedimentos = await sessao.scalars(
        select(Procedimento).where(Procedimento.clinica_id == clinica_id)
    )
    achados = [
        p for p in procedimentos if procurado in {normalizar(n) for n in (p.nome, *p.sinonimos)}
    ]
    if not achados:
        raise ErroAgenda("procedimento_nao_encontrado")
    if len(achados) > 1:
        raise ErroAgenda("procedimento_ambiguo")
    return achados[0]


def _palavras_do_nome(nome: str) -> set[str]:
    return set(re.findall(r"\w+", normalizar(nome))) - _TITULOS


async def resolver_dentista(sessao: AsyncSession, clinica_id: UUID, nome: str) -> Dentista:
    procuradas = _palavras_do_nome(nome)
    dentistas = (
        await sessao.scalars(
            select(Dentista).where(Dentista.clinica_id == clinica_id, Dentista.ativo)
        )
    ).all()
    achados = [d for d in dentistas if procuradas and procuradas <= _palavras_do_nome(d.nome)]
    # "Ana Souza" é a Ana Souza, mesmo que também exista uma Ana Souza Lima.
    exatos = [d for d in achados if procuradas == _palavras_do_nome(d.nome)]
    achados = exatos or achados
    if not achados:
        raise ErroAgenda("dentista_nao_encontrado")
    if len(achados) > 1:
        raise ErroAgenda("dentista_ambiguo")
    return achados[0]


async def _montar_opcoes(
    sessao: AsyncSession,
    clinica_id: UUID,
    candidatos: Sequence[_Candidato],
    fuso: ZoneInfo,
) -> list[Opcao]:
    escolhidos = escolher_variados(candidatos, fuso)
    linhas = await sessao.execute(
        select(Dentista.id, Dentista.nome).where(
            Dentista.clinica_id == clinica_id,
            Dentista.id.in_({c.dentista_id for c in escolhidos}),
        )
    )
    nomes = {id_: nome for id_, nome in linhas.all()}
    return [
        Opcao(
            dentista_id=c.dentista_id,
            dentista=nomes[c.dentista_id],
            inicio=c.inicio,
            fala=f"{falar_horario(c.inicio, fuso)}, com {nomes[c.dentista_id]}",
        )
        for c in escolhidos
    ]


async def listar_procedimentos(sessao: AsyncSession, contexto: Contexto) -> Procedimentos:
    procedimentos = await sessao.scalars(
        select(Procedimento)
        .where(Procedimento.clinica_id == contexto.clinica_id)
        .order_by(Procedimento.nome)
    )
    return Procedimentos(
        procedimentos=[ProcedimentoResumo.model_validate(p) for p in procedimentos]
    )


def _instante(valor: datetime | date, hora: time, fuso: ZoneInfo) -> datetime:
    return valor if isinstance(valor, datetime) else datetime.combine(valor, hora, tzinfo=fuso)


async def opcoes_horarios(
    sessao: AsyncSession,
    contexto: Contexto,
    config: ConfigAgenda,
    procedimento: str,
    a_partir_de: datetime | date,
    ate: datetime | date,
    dentista: str | None,
    turno: Turno | None,
) -> OpcoesHorario:
    fuso = await _fuso(sessao, contexto.clinica_id)
    resolvido = await resolver_procedimento(sessao, contexto.clinica_id, procedimento)
    dentista_id = None
    if dentista is not None:
        dentista_id = (await resolver_dentista(sessao, contexto.clinica_id, dentista)).id
    candidatos = await listar_horarios_livres(
        sessao,
        contexto.clinica_id,
        resolvido.id,
        _instante(a_partir_de, time.min, fuso),
        _instante(ate, time.max, fuso),
        config,
        dentista_id,
    )
    if turno is not None:
        candidatos = [c for c in candidatos if turno_de(c.inicio, fuso) == turno]
    return OpcoesHorario(opcoes=await _montar_opcoes(sessao, contexto.clinica_id, candidatos, fuso))


async def segurar_por_nome(
    sessao: AsyncSession,
    contexto: Contexto,
    entrada: SegurarPorNomeEntrada,
    chave: str,
    config: ConfigAgenda,
    publicador: PublicadorAgenda,
) -> HoldFalado:
    fuso = await _fuso(sessao, contexto.clinica_id)
    procedimento = await resolver_procedimento(sessao, contexto.clinica_id, entrada.procedimento)
    dentista = await resolver_dentista(sessao, contexto.clinica_id, entrada.dentista)
    nome_procedimento, nome_dentista = procedimento.nome, dentista.nome
    pedido = SegurarEntrada(
        procedimento_id=procedimento.id, dentista_id=dentista.id, inicio=entrada.inicio
    )
    # Encerra as leituras: o now() de segurar não deve vir de antes dele.
    await sessao.commit()
    try:
        hold = await agenda.segurar(sessao, contexto, pedido, chave, config, publicador)
    except ErroAgenda as erro:
        erro.opcoes = await _montar_opcoes(sessao, contexto.clinica_id, erro.alternativas, fuso)
        raise
    return HoldFalado(
        **hold.model_dump(),
        dentista=nome_dentista,
        procedimento=nome_procedimento,
        fala=f"{falar_horario(hold.inicio, fuso)}, com {nome_dentista}",
    )


async def listar_meus_agendamentos(sessao: AsyncSession, contexto: Contexto) -> ConsultasFuturas:
    if not contexto.voz:
        raise ErroAgenda("exige_chamada")
    agenda.exigir_identidade(contexto)
    fuso = await _fuso(sessao, contexto.clinica_id)
    linhas = await sessao.execute(
        select(Agendamento, Procedimento.nome, Dentista.nome)
        .join(Procedimento, Agendamento.procedimento_id == Procedimento.id)
        .join(Dentista, Agendamento.dentista_id == Dentista.id)
        .where(
            Agendamento.clinica_id == contexto.clinica_id,
            Agendamento.paciente_id == contexto.paciente_id,
            Agendamento.status == "confirmado",
            func.lower(Agendamento.periodo) > func.now(),
        )
        .order_by(func.lower(Agendamento.periodo))
    )
    consultas = []
    for consulta, procedimento, dentista in linhas.all():
        assert consulta.periodo.lower is not None
        inicio = consulta.periodo.lower
        consultas.append(
            ConsultaFutura(
                agendamento_id=consulta.id,
                procedimento=procedimento,
                dentista=dentista,
                inicio=inicio,
                fala=f"{procedimento}, {falar_horario(inicio, fuso)}, com {dentista}",
            )
        )
    return ConsultasFuturas(consultas=consultas)


async def _falar_consulta(sessao: AsyncSession, consulta: Agendamento, fuso: ZoneInfo) -> str:
    assert consulta.periodo.lower is not None
    procedimento = await sessao.get_one(Procedimento, consulta.procedimento_id)
    dentista = await sessao.get_one(Dentista, consulta.dentista_id)
    return (
        f"{procedimento.nome}, {falar_horario(consulta.periodo.lower, fuso)}, com {dentista.nome}"
    )


def _exigir_voz_verificada(contexto: Contexto) -> None:
    if not contexto.voz:
        raise ErroAgenda("exige_chamada")
    agenda.exigir_identidade(contexto)


async def preparar_cancelamento(
    sessao: AsyncSession, contexto: Contexto, agendamento_id: UUID
) -> CancelamentoPreparado:
    """Só lê: valida o cancelamento e devolve o que repetir ao paciente antes do sim."""
    _exigir_voz_verificada(contexto)
    consulta = await agenda.consulta_alteravel(sessao, contexto, agendamento_id)
    assert consulta.periodo.lower is not None
    clinica = await sessao.get_one(Clinica, contexto.clinica_id)
    horas = clinica.politica_cancelamento_horas
    agora = (await sessao.execute(select(func.now()))).scalar_one()
    return CancelamentoPreparado(
        agendamento_id=agendamento_id,
        fala=await _falar_consulta(sessao, consulta, ZoneInfo(clinica.fuso)),
        dentro_do_prazo=consulta.periodo.lower - agora >= timedelta(hours=horas),
        politica_cancelamento_horas=horas,
    )


async def preparar_remarcacao(
    sessao: AsyncSession, contexto: Contexto, agendamento_id: UUID, entrada: RemarcarEntrada
) -> RemarcacaoPreparada:
    """Só lê: valida a troca e devolve os dois horários para repetir ao paciente antes do sim."""
    _exigir_voz_verificada(contexto)
    consulta = await agenda.consulta_alteravel(sessao, contexto, agendamento_id)
    hold = await sessao.scalar(
        select(Agendamento).where(
            Agendamento.id == entrada.hold_id,
            Agendamento.clinica_id == contexto.clinica_id,
            Agendamento.chamada_id == contexto.chamada_id,
            Agendamento.status == "segurado",
            Agendamento.expira_em > func.now(),
        )
    )
    if hold is None:
        raise ErroAgenda("hold_perdido")
    fuso = await _fuso(sessao, contexto.clinica_id)
    return RemarcacaoPreparada(
        agendamento_id=agendamento_id,
        hold_id=entrada.hold_id,
        fala_atual=await _falar_consulta(sessao, consulta, fuso),
        fala_nova=await _falar_consulta(sessao, hold, fuso),
    )
