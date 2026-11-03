"""Rotas só do painel. A agenda usa as mesmas rotas da voz, com origem=painel."""

import asyncio
from datetime import timedelta
from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_api import auditoria
from clinica_api.agenda import ErroAgenda
from clinica_api.autenticacao import (
    Admin,
    Logado,
    Sessao,
    UsuarioAtual,
    gerar_hash,
    precisa_de_novo_hash,
    senha_confere,
    token_do_painel,
)
from clinica_api.esquemas_painel import (
    ConsultaDaAgenda,
    DentistaResumo,
    LoginEntrada,
    MudarStatusEntrada,
    PacienteEntrada,
    PacienteResumo,
    ProcedimentoCompleto,
    RegistroDeAuditoria,
    TrocarSenhaEntrada,
    UsuarioAtualizar,
    UsuarioCriar,
    UsuarioResumo,
)
from clinica_api.esquemas_painel import Sessao as SessaoDoPainel
from clinica_core.eventos import criar_evento
from clinica_core.modelos import (
    Agendamento,
    Auditoria,
    Clinica,
    Consultorio,
    Dentista,
    Paciente,
    Procedimento,
    Usuario,
)
from clinica_core.telefone import normalizar_telefone

router = APIRouter(prefix="/painel")

MAXIMO_FALHAS_DE_LOGIN = 5
TEMPO_DE_BLOQUEIO = timedelta(minutes=15)
MAXIMO_AUDITORIA = 200
MAXIMO_PACIENTES = 50
# Uma semana de agenda, com folga: um pedido não varre a tabela toda.
_JANELA_MAXIMA_DA_AGENDA = timedelta(days=45)


@router.post("/login", dependencies=[Depends(token_do_painel)])
async def login(entrada: LoginEntrada, sessao: Sessao) -> SessaoDoPainel:
    usuario = await sessao.scalar(
        select(Usuario).where(Usuario.email == entrada.email).with_for_update()
    )
    agora = (await sessao.execute(select(func.now()))).scalar_one()
    if usuario is not None and usuario.bloqueado_ate is not None and usuario.bloqueado_ate > agora:
        await sessao.rollback()
        raise ErroAgenda("login_bloqueado")
    senha_hash = usuario.senha_hash if usuario is not None else None
    # O argon2 gasta CPU de propósito: fora do laço de eventos.
    confere = await asyncio.to_thread(senha_confere, senha_hash, entrada.senha)
    if usuario is None or not usuario.ativo or not confere:
        if usuario is not None:
            usuario.falhas_de_login += 1
            if usuario.falhas_de_login >= MAXIMO_FALHAS_DE_LOGIN:
                usuario.bloqueado_ate = agora + TEMPO_DE_BLOQUEIO
                usuario.falhas_de_login = 0
            auditoria.registrar(sessao, usuario.clinica_id, usuario.id, "login_falhou", "usuario")
            await sessao.commit()
        raise ErroAgenda("credenciais_invalidas")
    usuario.falhas_de_login = 0
    usuario.bloqueado_ate = None
    if precisa_de_novo_hash(usuario.senha_hash):
        usuario.senha_hash = gerar_hash(entrada.senha)
    auditoria.registrar(sessao, usuario.clinica_id, usuario.id, "login", "usuario")
    clinica = await sessao.get_one(Clinica, usuario.clinica_id)
    resposta = SessaoDoPainel(
        usuario_id=usuario.id,
        clinica_id=usuario.clinica_id,
        email=usuario.email,
        perfil=usuario.perfil,
        clinica=clinica.nome,
        fuso=clinica.fuso,
    )
    await sessao.commit()
    return resposta


@router.post("/minha-senha", status_code=HTTPStatus.NO_CONTENT)
async def trocar_minha_senha(entrada: TrocarSenhaEntrada, sessao: Sessao, atual: Logado) -> None:
    usuario = await sessao.get_one(Usuario, atual.id)
    if not await asyncio.to_thread(senha_confere, usuario.senha_hash, entrada.atual):
        raise ErroAgenda("senha_atual_incorreta")
    usuario.senha_hash = await asyncio.to_thread(gerar_hash, entrada.nova)
    auditoria.registrar(sessao, atual.clinica_id, atual.id, "trocar_senha", "usuario")
    await sessao.commit()


async def _usuario_da_clinica(sessao: AsyncSession, admin: UsuarioAtual, id_: UUID) -> Usuario:
    usuario = await sessao.scalar(
        select(Usuario).where(Usuario.id == id_, Usuario.clinica_id == admin.clinica_id)
    )
    if usuario is None:
        raise ErroAgenda("registro_nao_encontrado")
    return usuario


async def _email_livre(sessao: AsyncSession, email: str, id_: UUID | None = None) -> None:
    dono = await sessao.scalar(select(Usuario.id).where(Usuario.email == email))
    if dono is not None and dono != id_:
        raise ErroAgenda("email_em_uso")


@router.get("/usuarios")
async def listar_usuarios(sessao: Sessao, admin: Admin) -> list[UsuarioResumo]:
    usuarios = await sessao.scalars(
        select(Usuario).where(Usuario.clinica_id == admin.clinica_id).order_by(Usuario.email)
    )
    return [UsuarioResumo.model_validate(u) for u in usuarios]


@router.post("/usuarios", status_code=HTTPStatus.CREATED)
async def criar_usuario(entrada: UsuarioCriar, sessao: Sessao, admin: Admin) -> UsuarioResumo:
    await _email_livre(sessao, entrada.email)
    usuario = Usuario(
        clinica_id=admin.clinica_id,
        email=entrada.email,
        senha_hash=await asyncio.to_thread(gerar_hash, entrada.senha),
        perfil=entrada.perfil,
    )
    sessao.add(usuario)
    await sessao.flush()
    await sessao.refresh(usuario)
    resposta = UsuarioResumo.model_validate(usuario)
    # Nunca a senha nem o hash.
    auditoria.registrar(
        sessao,
        admin.clinica_id,
        admin.id,
        "criar",
        "usuario",
        depois=resposta.model_dump(mode="json"),
    )
    await sessao.commit()
    return resposta


@router.put("/usuarios/{usuario_id}")
async def atualizar_usuario(
    usuario_id: UUID, entrada: UsuarioAtualizar, sessao: Sessao, admin: Admin
) -> UsuarioResumo:
    # A linha da clínica serializa dois administradores se rebaixando ao mesmo tempo.
    await sessao.execute(
        update(Clinica).where(Clinica.id == admin.clinica_id).values(nome=Clinica.nome)
    )
    usuario = await _usuario_da_clinica(sessao, admin, usuario_id)
    await _email_livre(sessao, entrada.email, usuario_id)
    antes = UsuarioResumo.model_validate(usuario).model_dump(mode="json")
    deixa_de_ser_admin = (
        usuario.perfil == "admin"
        and usuario.ativo
        and (entrada.perfil != "admin" or not entrada.ativo)
    )
    if deixa_de_ser_admin:
        outros = await sessao.scalar(
            select(func.count())
            .select_from(Usuario)
            .where(
                Usuario.clinica_id == admin.clinica_id,
                Usuario.perfil == "admin",
                Usuario.ativo,
                Usuario.id != usuario_id,
            )
        )
        if not outros:
            raise ErroAgenda("ultimo_admin")
    usuario.email = entrada.email
    usuario.perfil = entrada.perfil
    usuario.ativo = entrada.ativo
    if entrada.senha:
        usuario.senha_hash = await asyncio.to_thread(gerar_hash, entrada.senha)
    await sessao.flush()
    resposta = UsuarioResumo.model_validate(usuario)
    auditoria.registrar(
        sessao,
        admin.clinica_id,
        admin.id,
        "atualizar",
        "usuario",
        antes=antes,
        depois={**resposta.model_dump(mode="json"), "senha_trocada": bool(entrada.senha)},
    )
    await sessao.commit()
    return resposta


@router.get("/auditoria")
async def listar_auditoria(
    sessao: Sessao,
    admin: Admin,
    limite: Annotated[int, Query(ge=1, le=MAXIMO_AUDITORIA)] = MAXIMO_AUDITORIA,
) -> list[RegistroDeAuditoria]:
    linhas = await sessao.execute(
        select(Auditoria, Usuario.email)
        .outerjoin(Usuario, Auditoria.usuario_id == Usuario.id)
        .where(Auditoria.clinica_id == admin.clinica_id)
        .order_by(Auditoria.criado_em.desc())
        .limit(limite)
    )
    return [
        RegistroDeAuditoria(
            criado_em=registro.criado_em,
            usuario=email,
            acao=registro.acao,
            entidade=registro.entidade,
            antes=registro.antes,
            depois=registro.depois,
        )
        for registro, email in linhas.all()
    ]


def _consultas_da_clinica(clinica_id: UUID):  # type: ignore[no-untyped-def]
    return (
        select(
            Agendamento,
            Dentista.nome,
            Consultorio.nome,
            Procedimento.nome,
            Paciente.nome,
            Paciente.telefone_e164,
        )
        .join(Dentista, Agendamento.dentista_id == Dentista.id)
        .join(Consultorio, Agendamento.consultorio_id == Consultorio.id)
        .join(Procedimento, Agendamento.procedimento_id == Procedimento.id)
        .outerjoin(Paciente, Agendamento.paciente_id == Paciente.id)
        .where(Agendamento.clinica_id == clinica_id)
    )


def _consulta(linha: Any) -> ConsultaDaAgenda:
    agendamento, dentista, consultorio, procedimento, paciente, telefone = linha
    assert agendamento.periodo.lower is not None
    assert agendamento.periodo.upper is not None
    return ConsultaDaAgenda(
        id=agendamento.id,
        dentista_id=agendamento.dentista_id,
        dentista=dentista,
        consultorio=consultorio,
        procedimento_id=agendamento.procedimento_id,
        procedimento=procedimento,
        paciente_id=agendamento.paciente_id,
        paciente=paciente,
        telefone=telefone,
        inicio=agendamento.periodo.lower,
        fim_periodo=agendamento.periodo.upper,
        status=agendamento.status,
        origem=agendamento.origem,
        motivo_cancelamento=agendamento.motivo_cancelamento,
    )


@router.get("/agenda")
async def agenda(
    sessao: Sessao,
    atual: Logado,
    inicio: AwareDatetime,
    fim: AwareDatetime,
    dentista_id: UUID | None = None,
) -> list[ConsultaDaAgenda]:
    if not timedelta(0) < fim - inicio <= _JANELA_MAXIMA_DA_AGENDA:
        raise ErroAgenda("periodo_invalido")
    consulta = (
        _consultas_da_clinica(atual.clinica_id)
        .where(
            Agendamento.periodo.op("&&")(Range(inicio, fim)),
            Agendamento.status != "expirado",
            # Vencido não ocupa o horário, mesmo antes de o worker marcar como expirado.
            or_(Agendamento.status != "segurado", Agendamento.expira_em > func.now()),
        )
        .order_by(func.lower(Agendamento.periodo), Dentista.nome)
    )
    if dentista_id is not None:
        consulta = consulta.where(Agendamento.dentista_id == dentista_id)
    return [_consulta(linha) for linha in (await sessao.execute(consulta)).all()]


@router.get("/agendamentos/{agendamento_id}")
async def ver_consulta(agendamento_id: UUID, sessao: Sessao, atual: Logado) -> ConsultaDaAgenda:
    linha = (
        await sessao.execute(
            _consultas_da_clinica(atual.clinica_id).where(Agendamento.id == agendamento_id)
        )
    ).first()
    if linha is None:
        raise ErroAgenda("registro_nao_encontrado")
    return _consulta(linha)


@router.post("/agendamentos/{agendamento_id}/status")
async def mudar_status(
    agendamento_id: UUID,
    entrada: MudarStatusEntrada,
    sessao: Sessao,
    atual: Logado,
    request: Request,
) -> dict[str, str]:
    agendamento = await sessao.scalar(
        update(Agendamento)
        .where(
            Agendamento.id == agendamento_id,
            Agendamento.clinica_id == atual.clinica_id,
            Agendamento.status == "confirmado",
        )
        .values(status=entrada.status)
        .returning(Agendamento)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    if agendamento is None:
        raise ErroAgenda("agendamento_nao_ativo")
    evento = criar_evento(agendamento, "atualizado")
    auditoria.registrar(
        sessao,
        atual.clinica_id,
        atual.id,
        "mudar_status",
        "agendamento",
        antes={"id": str(agendamento_id), "status": "confirmado"},
        depois={"id": str(agendamento_id), "status": entrada.status},
    )
    await sessao.commit()
    await request.app.state.publicador.eventos([evento])
    return {"status": entrada.status}


def _telefone_valido(bruto: str) -> str:
    telefone = normalizar_telefone(bruto)
    if telefone is None:
        raise ErroAgenda("telefone_invalido")
    return telefone


def _sem_dados_do_paciente(paciente: Paciente) -> dict[str, str]:
    # A auditoria diz quem mexeu em qual paciente, sem copiar nome, telefone ou nascimento.
    return {"id": str(paciente.id)}


@router.get("/pacientes")
async def listar_pacientes(
    sessao: Sessao, atual: Logado, busca: str | None = None
) -> list[PacienteResumo]:
    consulta = select(Paciente).where(Paciente.clinica_id == atual.clinica_id)
    if busca:
        termo = busca.strip()
        digitos = "".join(c for c in termo if c.isdigit())
        filtros = [Paciente.nome_normalizado.contains(func.normalizar_nome(termo))]
        if digitos:
            filtros.append(Paciente.telefone_e164.contains(digitos))
        consulta = consulta.where(or_(*filtros))
    pacientes = await sessao.scalars(consulta.order_by(Paciente.nome).limit(MAXIMO_PACIENTES))
    resposta = [PacienteResumo.model_validate(p) for p in pacientes]
    # Sem o termo: ele costuma ser o nome de alguém.
    auditoria.registrar(
        sessao,
        atual.clinica_id,
        atual.id,
        "ver",
        "paciente",
        depois={"quantidade": len(resposta), "com_busca": bool(busca)},
    )
    await sessao.commit()
    return resposta


async def _gravar_paciente(sessao: AsyncSession) -> None:
    try:
        await sessao.flush()
    except IntegrityError as erro:
        await sessao.rollback()
        raise ErroAgenda("paciente_duplicado") from erro


@router.get("/pacientes/{paciente_id}")
async def ver_paciente(paciente_id: UUID, sessao: Sessao, atual: Logado) -> PacienteResumo:
    paciente = await sessao.scalar(
        select(Paciente).where(Paciente.id == paciente_id, Paciente.clinica_id == atual.clinica_id)
    )
    if paciente is None:
        raise ErroAgenda("registro_nao_encontrado")
    resposta = PacienteResumo.model_validate(paciente)
    auditoria.registrar(
        sessao,
        atual.clinica_id,
        atual.id,
        "ver",
        "paciente",
        depois=_sem_dados_do_paciente(paciente),
    )
    await sessao.commit()
    return resposta


@router.post("/pacientes", status_code=HTTPStatus.CREATED)
async def criar_paciente(entrada: PacienteEntrada, sessao: Sessao, atual: Logado) -> PacienteResumo:
    paciente = Paciente(
        clinica_id=atual.clinica_id,
        nome=entrada.nome,
        telefone_e164=_telefone_valido(entrada.telefone_e164),
        data_nascimento=entrada.data_nascimento,
    )
    sessao.add(paciente)
    await _gravar_paciente(sessao)
    resposta = PacienteResumo.model_validate(paciente)
    auditoria.registrar(
        sessao,
        atual.clinica_id,
        atual.id,
        "criar",
        "paciente",
        depois=_sem_dados_do_paciente(paciente),
    )
    await sessao.commit()
    return resposta


@router.put("/pacientes/{paciente_id}")
async def atualizar_paciente(
    paciente_id: UUID, entrada: PacienteEntrada, sessao: Sessao, atual: Logado
) -> PacienteResumo:
    paciente = await sessao.scalar(
        select(Paciente).where(Paciente.id == paciente_id, Paciente.clinica_id == atual.clinica_id)
    )
    if paciente is None:
        raise ErroAgenda("registro_nao_encontrado")
    paciente.nome = entrada.nome
    paciente.telefone_e164 = _telefone_valido(entrada.telefone_e164)
    paciente.data_nascimento = entrada.data_nascimento
    await _gravar_paciente(sessao)
    resposta = PacienteResumo.model_validate(paciente)
    auditoria.registrar(
        sessao,
        atual.clinica_id,
        atual.id,
        "atualizar",
        "paciente",
        depois=_sem_dados_do_paciente(paciente),
    )
    await sessao.commit()
    return resposta


@router.get("/dentistas")
async def listar_dentistas(sessao: Sessao, atual: Logado) -> list[DentistaResumo]:
    dentistas = await sessao.scalars(
        select(Dentista).where(Dentista.clinica_id == atual.clinica_id).order_by(Dentista.nome)
    )
    return [DentistaResumo.model_validate(d) for d in dentistas]


@router.get("/procedimentos")
async def listar_procedimentos(sessao: Sessao, atual: Logado) -> list[ProcedimentoCompleto]:
    procedimentos = await sessao.scalars(
        select(Procedimento)
        .where(Procedimento.clinica_id == atual.clinica_id)
        .order_by(Procedimento.nome)
    )
    return [ProcedimentoCompleto.model_validate(p) for p in procedimentos]
