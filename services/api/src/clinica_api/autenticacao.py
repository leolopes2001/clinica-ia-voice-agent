import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from clinica_api.agenda import ErroAgenda
from clinica_core.config import ConfigTokens
from clinica_core.modelos import Usuario

_senhas = PasswordHasher()
# Sem usuário, a verificação gasta o mesmo tempo: o tempo não revela quais e-mails existem.
_HASH_FALSO = _senhas.hash("senha-que-nao-pertence-a-ninguem")


def gerar_hash(senha: str) -> str:
    return _senhas.hash(senha)


def senha_confere(senha_hash: str | None, senha: str) -> bool:
    try:
        return _senhas.verify(senha_hash or _HASH_FALSO, senha) and senha_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def precisa_de_novo_hash(senha_hash: str) -> bool:
    return _senhas.check_needs_rehash(senha_hash)


def papel_do_token(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
    tokens: ConfigTokens = request.app.state.config_tokens
    esquema, _, token = (authorization or "").partition(" ")
    if esquema.lower() == "bearer":
        recebido = token.encode()
        if secrets.compare_digest(recebido, tokens.painel.get_secret_value().encode()):
            return "painel"
        if secrets.compare_digest(recebido, tokens.servicos.get_secret_value().encode()):
            return "servicos"
    raise ErroAgenda("nao_autenticado")


def exigir_token(papel: str, exigido: str) -> None:
    if papel != exigido:
        raise ErroAgenda("token_nao_permitido")


def token_do_painel(papel: Annotated[str, Depends(papel_do_token)]) -> None:
    exigir_token(papel, "painel")


async def _sessao(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.fabrica_sessoes() as sessao:
        yield sessao


Sessao = Annotated[AsyncSession, Depends(_sessao)]


@dataclass(frozen=True)
class UsuarioAtual:
    id: UUID
    clinica_id: UUID
    perfil: str

    @property
    def admin(self) -> bool:
        return self.perfil == "admin"


async def usuario_atual(
    sessao: Sessao,
    _: Annotated[None, Depends(token_do_painel)],
    x_usuario_id: Annotated[UUID | None, Header()] = None,
) -> UsuarioAtual:
    # Lê do banco a cada pedido: desativar ou mudar o perfil vale na hora.
    usuario = await sessao.get(Usuario, x_usuario_id) if x_usuario_id else None
    if usuario is None or not usuario.ativo:
        raise ErroAgenda("nao_autenticado")
    atual = UsuarioAtual(usuario.id, usuario.clinica_id, usuario.perfil)
    await sessao.commit()
    return atual


def exigir_admin(usuario: Annotated[UsuarioAtual, Depends(usuario_atual)]) -> UsuarioAtual:
    if not usuario.admin:
        raise ErroAgenda("sem_permissao")
    return usuario


Logado = Annotated[UsuarioAtual, Depends(usuario_atual)]
Admin = Annotated[UsuarioAtual, Depends(exigir_admin)]
