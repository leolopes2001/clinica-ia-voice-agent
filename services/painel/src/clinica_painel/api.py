"""Cliente da API. O painel não toca no banco: toda regra e toda auditoria ficam lá."""

from typing import Any

import httpx
from fastapi import Request

MENSAGENS = {
    "agendamento_nao_ativo": "Esta consulta não está mais ativa.",
    "agendamento_passado": "Esta consulta já começou ou já passou.",
    "credenciais_invalidas": "E-mail ou senha incorretos.",
    "em_uso": "Este registro está em uso e não pode ser apagado. Desative-o, se for o caso.",
    "email_em_uso": "Este e-mail já está em uso.",
    "entrada_invalida": "Confira os campos preenchidos.",
    "grade_sobreposta": "Este horário se sobrepõe a outro do mesmo dentista ou consultório.",
    "hold_perdido": "O horário se perdeu. Escolha outro.",
    "indisponivel": "Este horário não está disponível.",
    "login_bloqueado": "Muitas tentativas erradas. Espere 15 minutos e tente de novo.",
    "ocupado": "Esse horário acabou de ser ocupado. Escolha outro.",
    "paciente_duplicado": "Já existe esse paciente neste telefone.",
    "paciente_nao_encontrado": "Paciente não encontrado.",
    "telefone_invalido": "Telefone inválido. Informe com o DDD, como 11 98888-7777.",
    "registro_nao_encontrado": "Registro não encontrado.",
    "sem_permissao": "Seu perfil não tem acesso a esta ação.",
    "senha_atual_incorreta": "A senha atual está incorreta.",
    "servico_indisponivel": "O sistema está com dificuldade. Tente de novo em instantes.",
    "ultimo_admin": "A clínica precisa de pelo menos um administrador ativo.",
}


class ErroDaApi(Exception):
    def __init__(self, status: int, motivo: str) -> None:
        super().__init__(motivo)
        self.status = status
        self.motivo = motivo

    @property
    def mensagem(self) -> str:
        return MENSAGENS.get(self.motivo, "Não foi possível concluir. Confira e tente de novo.")


def _motivo(resposta: httpx.Response) -> str:
    if resposta.is_server_error:
        return "servico_indisponivel"
    try:
        corpo = resposta.json()
    except ValueError:
        return "servico_indisponivel"
    # Erro de validação do FastAPI, em que "detail" não é um motivo.
    return str(corpo.get("motivo", "entrada_invalida"))


async def chamar(
    request: Request,
    metodo: str,
    caminho: str,
    *,
    json: Any = None,
    params: dict[str, Any] | None = None,
    chave: str | None = None,
) -> Any:
    """Fala com a API em nome do usuário da sessão. O token do painel já vem no cliente."""
    api: httpx.AsyncClient = request.app.state.api
    cabecalhos: dict[str, str] = {}
    sessao = request.session
    if "usuario_id" in sessao:
        cabecalhos["X-Usuario-Id"] = sessao["usuario_id"]
        cabecalhos["X-Clinica-Id"] = sessao["clinica_id"]
    if chave is not None:
        cabecalhos["Idempotency-Key"] = chave
    try:
        resposta = await api.request(
            metodo,
            caminho,
            headers=cabecalhos,
            json=json,
            params={k: v for k, v in (params or {}).items() if v not in (None, "")},
        )
    except httpx.HTTPError as erro:
        raise ErroDaApi(503, "servico_indisponivel") from erro
    if not resposta.is_success:
        raise ErroDaApi(resposta.status_code, _motivo(resposta))
    return resposta.json() if resposta.content else None
