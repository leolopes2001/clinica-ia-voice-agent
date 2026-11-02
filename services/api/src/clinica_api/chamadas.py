import secrets
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_api import idempotencia
from clinica_api.agenda import Contexto, ErroAgenda, compor_chave, transacao
from clinica_api.esquemas import (
    CadastrarPacienteEntrada,
    ChamadaCriada,
    ClinicaResumo,
    Clinicas,
    ConsentimentoEntrada,
    ConsentimentoRegistrado,
    CriarChamadaEntrada,
    PacienteDaChamada,
    PacientesDoTelefone,
    PedidoRetornoCriado,
    PedidoRetornoEntrada,
    ResultadoVerificacao,
    SelecionarPacienteEntrada,
    VerificarIdentidadeEntrada,
)
from clinica_api.travas import travar
from clinica_core.modelos import Chamada, Clinica, Paciente, PedidoRetorno

MAXIMO_TENTATIVAS_IDENTIDADE = 3
_TENTATIVAS_DE_PROTOCOLO = 5


def _exigir_chamada(contexto: Contexto) -> UUID:
    if contexto.chamada_id is None:
        raise ErroAgenda("exige_chamada")
    return contexto.chamada_id


async def _chamada_travada(sessao: AsyncSession, chamada_id: UUID) -> Chamada:
    # A linha travada serializa a seleção e as tentativas de uma mesma chamada.
    return (
        await sessao.scalars(
            select(Chamada)
            .where(Chamada.id == chamada_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one()


def _resumo(paciente: Paciente) -> PacienteDaChamada:
    return PacienteDaChamada(paciente_id=paciente.id, primeiro_nome=paciente.nome.split()[0])


def _trocar_paciente(chamada: Chamada, paciente_id: UUID) -> None:
    # Trocar de paciente desfaz a verificação, mas não zera as tentativas.
    if chamada.paciente_id != paciente_id:
        chamada.paciente_id = paciente_id
        chamada.identidade_verificada_em = None


async def listar_clinicas(sessao: AsyncSession) -> Clinicas:
    linhas = await sessao.execute(select(Clinica.id, Clinica.nome).order_by(Clinica.nome))
    return Clinicas(clinicas=[ClinicaResumo(clinica_id=i, nome=n) for i, n in linhas.all()])


async def criar_chamada(
    sessao: AsyncSession, contexto: Contexto, entrada: CriarChamadaEntrada, chave: str
) -> ChamadaCriada:
    if contexto.voz:
        raise ErroAgenda("exige_painel")
    chave_completa = compor_chave(contexto, "chamada", chave)
    async with transacao(sessao):
        await travar(sessao, chave_completa)
        guardada = await idempotencia.buscar(sessao, chave_completa)
        if guardada is not None:
            return ChamadaCriada.model_validate(guardada)
        chamada = Chamada(
            clinica_id=contexto.clinica_id, telefone=entrada.telefone, sala=entrada.sala
        )
        sessao.add(chamada)
        await sessao.flush()
        resposta = ChamadaCriada(chamada_id=chamada.id)
        await idempotencia.guardar(sessao, chave_completa, resposta.model_dump(mode="json"))
    return resposta


async def registrar_consentimento(
    sessao: AsyncSession, contexto: Contexto, entrada: ConsentimentoEntrada
) -> ConsentimentoRegistrado:
    chamada_id = _exigir_chamada(contexto)
    async with transacao(sessao):
        chamada = await _chamada_travada(sessao, chamada_id)
        # A recusa vale até o fim da chamada, e um "sim" mal entendido não a desfaz.
        if entrada.aceitou and chamada.consentimento_recusado_em is None:
            if chamada.consentimento_em is None:
                chamada.consentimento_em = func.now()
            return ConsentimentoRegistrado(consentimento=True)
        if chamada.consentimento_recusado_em is None:
            chamada.consentimento_recusado_em = func.now()
        chamada.consentimento_em = None
        chamada.paciente_id = None
        chamada.identidade_verificada_em = None
    return ConsentimentoRegistrado(consentimento=False)


async def identificar(sessao: AsyncSession, contexto: Contexto) -> PacientesDoTelefone:
    chamada_id = _exigir_chamada(contexto)
    async with transacao(sessao):
        chamada = await _chamada_travada(sessao, chamada_id)
        pacientes = (
            await sessao.scalars(
                select(Paciente)
                .where(
                    Paciente.clinica_id == chamada.clinica_id,
                    Paciente.telefone_e164 == chamada.telefone,
                )
                .order_by(Paciente.nome, Paciente.id)
            )
        ).all()
        if len(pacientes) == 1:
            _trocar_paciente(chamada, pacientes[0].id)
    return PacientesDoTelefone(pacientes=[_resumo(p) for p in pacientes])


async def selecionar(
    sessao: AsyncSession, contexto: Contexto, entrada: SelecionarPacienteEntrada
) -> PacienteDaChamada:
    chamada_id = _exigir_chamada(contexto)
    async with transacao(sessao):
        chamada = await _chamada_travada(sessao, chamada_id)
        paciente = await sessao.scalar(
            select(Paciente).where(
                Paciente.id == entrada.paciente_id,
                Paciente.clinica_id == chamada.clinica_id,
                Paciente.telefone_e164 == chamada.telefone,
            )
        )
        if paciente is None:
            raise ErroAgenda("paciente_nao_permitido")
        chamada.paciente_id = paciente.id
        chamada.identidade_verificada_em = None
        resposta = _resumo(paciente)
    return resposta


async def cadastrar(
    sessao: AsyncSession, contexto: Contexto, entrada: CadastrarPacienteEntrada
) -> PacienteDaChamada:
    chamada_id = _exigir_chamada(contexto)
    async with transacao(sessao):
        chamada = await _chamada_travada(sessao, chamada_id)
        hoje = (await sessao.execute(select(func.current_date()))).scalar_one()
        if entrada.data_nascimento > hoje:
            raise ErroAgenda("data_nascimento_invalida")
        # A mesma pessoa não é cadastrada duas vezes no mesmo telefone.
        await sessao.execute(
            insert(Paciente)
            .values(
                clinica_id=chamada.clinica_id,
                nome=entrada.nome,
                telefone_e164=chamada.telefone,
                data_nascimento=entrada.data_nascimento,
                consentimento_em=chamada.consentimento_em,
            )
            .on_conflict_do_nothing(constraint="uq_pacientes_mesma_pessoa")
        )
        paciente = (
            await sessao.scalars(
                select(Paciente).where(
                    Paciente.clinica_id == chamada.clinica_id,
                    Paciente.telefone_e164 == chamada.telefone,
                    Paciente.nome_normalizado == func.normalizar_nome(entrada.nome),
                    Paciente.data_nascimento == entrada.data_nascimento,
                )
            )
        ).one()
        if paciente.consentimento_em is None:
            paciente.consentimento_em = chamada.consentimento_em
        _trocar_paciente(chamada, paciente.id)
        resposta = _resumo(paciente)
    return resposta


async def verificar(
    sessao: AsyncSession, contexto: Contexto, entrada: VerificarIdentidadeEntrada
) -> ResultadoVerificacao:
    chamada_id = _exigir_chamada(contexto)
    async with transacao(sessao):
        chamada = await _chamada_travada(sessao, chamada_id)
        if chamada.paciente_id is None:
            raise ErroAgenda("paciente_nao_selecionado")
        if chamada.tentativas_identidade >= MAXIMO_TENTATIVAS_IDENTIDADE:
            return ResultadoVerificacao(
                ok=False, tentativas_restantes=0, motivo="tentativas_esgotadas"
            )
        nascimento = await sessao.scalar(
            select(Paciente.data_nascimento).where(Paciente.id == chamada.paciente_id)
        )
        ok = nascimento == entrada.data_nascimento
        if ok:
            chamada.identidade_verificada_em = func.now()
        else:
            chamada.tentativas_identidade += 1
            chamada.identidade_verificada_em = None
        restantes = MAXIMO_TENTATIVAS_IDENTIDADE - chamada.tentativas_identidade
    return ResultadoVerificacao(ok=ok, tentativas_restantes=restantes)


async def registrar_pedido_retorno(
    sessao: AsyncSession, contexto: Contexto, entrada: PedidoRetornoEntrada, chave: str
) -> PedidoRetornoCriado:
    chamada_id = _exigir_chamada(contexto)
    chave_completa = compor_chave(contexto, "pedido_retorno", chave)
    async with transacao(sessao):
        await travar(sessao, chave_completa)
        guardada = await idempotencia.buscar(sessao, chave_completa)
        if guardada is not None:
            return PedidoRetornoCriado.model_validate(guardada)
        chamada = await _chamada_travada(sessao, chamada_id)
        for _ in range(_TENTATIVAS_DE_PROTOCOLO):
            # Só dígitos: o agente fala o protocolo ao paciente.
            protocolo = f"{secrets.randbelow(10**8):08d}"
            criado = await sessao.scalar(
                insert(PedidoRetorno)
                .values(
                    clinica_id=chamada.clinica_id,
                    protocolo=protocolo,
                    chamada_id=chamada.id,
                    telefone=chamada.telefone,
                    motivo=entrada.motivo,
                )
                .on_conflict_do_nothing(constraint="uq_pedidos_retorno_protocolo")
                .returning(PedidoRetorno.id)
            )
            if criado is not None:
                break
        else:
            raise RuntimeError("Não foi possível gerar um protocolo livre")
        resposta = PedidoRetornoCriado(protocolo=protocolo)
        await idempotencia.guardar(sessao, chave_completa, resposta.model_dump(mode="json"))
    return resposta
