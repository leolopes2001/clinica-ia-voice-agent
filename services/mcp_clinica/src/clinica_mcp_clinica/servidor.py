import hashlib
import json
import logging
from datetime import date
from http import HTTPStatus
from typing import Any, Literal
from uuid import UUID

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import AwareDatetime
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_CABECALHO_CHAMADA = "x-chamada-id"
_CABECALHO_CLINICA = "x-clinica-id"
_INSTRUCOES = (
    "Agenda da clínica. A identidade do chamador e a clínica vêm do contexto da chamada: "
    "nenhuma ferramenta recebe telefone nem clínica. Antes de confirmar, remarcar ou cancelar, "
    "repita os dados ao paciente e espere um sim explícito."
)


def _chave_idempotencia(chamada_id: str, ferramenta: str, argumentos: dict[str, Any]) -> str:
    conteudo = json.dumps([chamada_id, ferramenta, argumentos], sort_keys=True, default=str)
    return hashlib.sha256(conteudo.encode()).hexdigest()


def _falha(resposta: httpx.Response) -> dict[str, Any]:
    if resposta.status_code == HTTPStatus.UNPROCESSABLE_ENTITY:
        corpo = resposta.json()
        # Erro de validação do FastAPI, em que "detail" não é um motivo da agenda.
        if "motivo" not in corpo:
            return {"ok": False, "motivo": "entrada_invalida"}
    if resposta.is_server_error:
        logger.warning("API respondeu %s", resposta.status_code)
        return {"ok": False, "motivo": "servico_indisponivel"}
    corpo = resposta.json()
    falha = {"ok": False, "motivo": corpo.get("motivo", "erro_desconhecido")}
    if corpo.get("opcoes"):
        falha["alternativas"] = corpo["opcoes"]
    return falha


def criar_servidor(api: httpx.AsyncClient) -> MCPServer:
    """Adaptador fino: as regras de negócio ficam na API."""
    mcp: MCPServer = MCPServer("mcp-clinica", instructions=_INSTRUCOES)

    async def chamar(
        ctx: Context,
        metodo: str,
        caminho: str,
        *,
        corpo: dict[str, Any] | None = None,
        consulta: dict[str, Any] | None = None,
        ferramenta_idempotente: str | None = None,
    ) -> dict[str, Any]:
        recebidos = ctx.headers or {}
        chamada_id = recebidos.get(_CABECALHO_CHAMADA)
        if not chamada_id:
            raise ToolError("Conexão sem o header X-Chamada-Id")
        cabecalhos = {"X-Chamada-Id": chamada_id}
        if clinica_id := recebidos.get(_CABECALHO_CLINICA):
            cabecalhos["X-Clinica-Id"] = clinica_id
        if ferramenta_idempotente is not None:
            cabecalhos["Idempotency-Key"] = _chave_idempotencia(
                chamada_id, ferramenta_idempotente, {"corpo": corpo, "caminho": caminho}
            )
        try:
            resposta = await api.request(
                metodo,
                caminho,
                headers=cabecalhos,
                json=corpo,
                params={k: v for k, v in (consulta or {}).items() if v is not None},
            )
        except httpx.HTTPError as erro:
            logger.warning("API indisponível: %r", erro)
            return {"ok": False, "motivo": "servico_indisponivel"}
        if not resposta.is_success:
            return _falha(resposta)
        retorno: dict[str, Any] = resposta.json()
        retorno.setdefault("ok", True)
        return retorno

    @mcp.tool()
    async def identificar_chamador(ctx: Context) -> dict[str, Any]:
        """Lista os pacientes do telefone da ligação: paciente_id e primeiro_nome.

        A lista vem vazia se o telefone não tiver cadastro. Com um único paciente, ele já fica
        selecionado na chamada. Com mais de um, pergunte para quem é a consulta e use
        selecionar_paciente.
        """
        return await chamar(ctx, "POST", "/identidade/identificar")

    @mcp.tool()
    async def selecionar_paciente(paciente_id: UUID, ctx: Context) -> dict[str, Any]:
        """Seleciona na chamada um paciente da lista de identificar_chamador.

        Desfaz qualquer verificação de identidade anterior.
        """
        return await chamar(
            ctx, "POST", "/identidade/selecionar", corpo={"paciente_id": str(paciente_id)}
        )

    @mcp.tool()
    async def cadastrar_paciente(nome: str, data_nascimento: date, ctx: Context) -> dict[str, Any]:
        """Cadastra um paciente novo neste telefone, que fica selecionado na chamada.

        Peça o nome completo e a data de nascimento (AAAA-MM-DD). O telefone vem da ligação.
        """
        return await chamar(
            ctx,
            "POST",
            "/identidade/cadastrar",
            corpo={"nome": nome, "data_nascimento": data_nascimento.isoformat()},
        )

    @mcp.tool()
    async def verificar_identidade(data_nascimento: date, ctx: Context) -> dict[str, Any]:
        """Confere a data de nascimento (AAAA-MM-DD) do paciente selecionado.

        Devolve ok true ou false e as tentativas restantes. São 3 erros por chamada: depois disso,
        o motivo é tentativas_esgotadas, e o agente deve oferecer a transferência para a recepção.
        """
        return await chamar(
            ctx,
            "POST",
            "/identidade/verificar",
            corpo={"data_nascimento": data_nascimento.isoformat()},
        )

    @mcp.tool()
    async def listar_procedimentos(ctx: Context) -> dict[str, Any]:
        """Lista os procedimentos da clínica: nome, duração em minutos e sinônimos."""
        return await chamar(ctx, "GET", "/procedimentos")

    @mcp.tool()
    async def listar_horarios_livres(
        procedimento: str,
        a_partir_de: str,
        ate: str,
        ctx: Context,
        dentista: str | None = None,
        turno: Literal["manha", "tarde", "noite"] | None = None,
    ) -> dict[str, Any]:
        """Devolve até 3 opções variadas de horário, cada uma com o texto pronto para falar.

        procedimento é o nome ou um sinônimo. a_partir_de e ate são datas (AAAA-MM-DD), ou data e
        hora com fuso. dentista é opcional. O turno vale pelo horário de início: manhã até 12h,
        tarde das 12h às 18h e noite a partir das 18h.
        """
        return await chamar(
            ctx,
            "GET",
            "/opcoes-horarios",
            consulta={
                "procedimento": procedimento,
                "a_partir_de": a_partir_de,
                "ate": ate,
                "dentista": dentista,
                "turno": turno,
            },
        )

    @mcp.tool()
    async def segurar_horario(
        procedimento: str, dentista: str, inicio: AwareDatetime, ctx: Context
    ) -> dict[str, Any]:
        """Segura um horário enquanto o paciente confirma, e devolve hold_id e expira_em.

        Use o inicio e o dentista de uma opção de listar_horarios_livres. Se o motivo for ocupado,
        diga que esse horário acabou de ser ocupado e ofereça as alternativas na mesma frase.
        """
        return await chamar(
            ctx,
            "POST",
            "/holds/por-nome",
            corpo={
                "procedimento": procedimento,
                "dentista": dentista,
                "inicio": inicio.isoformat(),
            },
            ferramenta_idempotente="segurar_horario",
        )

    @mcp.tool()
    async def confirmar_agendamento(hold_id: UUID, ctx: Context) -> dict[str, Any]:
        """Confirma a consulta segurada, para o paciente selecionado na chamada.

        Só use depois de repetir data, hora, profissional e procedimento e ouvir um sim explícito.
        O motivo hold_perdido indica que o horário se perdeu: ofereça novas opções.
        """
        return await chamar(
            ctx,
            "POST",
            f"/holds/{hold_id}/confirmar",
            corpo={},
            ferramenta_idempotente="confirmar_agendamento",
        )

    @mcp.tool()
    async def listar_meus_agendamentos(ctx: Context) -> dict[str, Any]:
        """Lista as consultas futuras do paciente selecionado. Exige identidade verificada."""
        return await chamar(ctx, "GET", "/agendamentos/meus")

    @mcp.tool()
    async def remarcar_agendamento(
        agendamento_id: UUID, hold_id: UUID, ctx: Context
    ) -> dict[str, Any]:
        """Troca uma consulta pelo novo horário já segurado, numa única operação.

        Exige identidade verificada e um sim explícito do paciente. O horário antigo só é liberado
        junto com a confirmação do novo.
        """
        return await chamar(
            ctx,
            "POST",
            f"/agendamentos/{agendamento_id}/remarcar",
            corpo={"hold_id": str(hold_id)},
            ferramenta_idempotente="remarcar_agendamento",
        )

    @mcp.tool()
    async def cancelar_agendamento(
        agendamento_id: UUID, ctx: Context, motivo: str | None = None
    ) -> dict[str, Any]:
        """Cancela uma consulta e devolve o aviso sobre a política de cancelamento.

        Exige identidade verificada e um sim explícito do paciente. Registre o motivo se o paciente
        disser.
        """
        return await chamar(
            ctx,
            "POST",
            f"/agendamentos/{agendamento_id}/cancelar",
            corpo={"motivo": motivo},
            ferramenta_idempotente="cancelar_agendamento",
        )

    @mcp.tool()
    async def registrar_pedido_retorno(motivo: str, ctx: Context) -> dict[str, Any]:
        """Registra um pedido para a clínica retornar a ligação, e devolve o protocolo."""
        return await chamar(
            ctx,
            "POST",
            "/pedidos-retorno",
            corpo={"motivo": motivo},
            ferramenta_idempotente="registrar_pedido_retorno",
        )

    @mcp.custom_route("/saude", methods=["GET"])  # type: ignore[untyped-decorator]
    async def saude(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return mcp


def criar_app(api: httpx.AsyncClient) -> Starlette:
    # host fora de loopback: a proteção de DNS rebinding recusaria o nome do serviço no Compose.
    return criar_servidor(api).streamable_http_app(
        stateless_http=True, json_response=True, host="0.0.0.0"
    )
