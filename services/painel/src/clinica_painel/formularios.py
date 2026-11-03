"""Cadastros descritos como dados: a mesma tela serve a todos."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

Opcoes = Callable[[Request], Awaitable[list[tuple[str, str]]]]


@dataclass(frozen=True)
class Campo:
    nome: str
    rotulo: str
    # text, email, password, number, date, time, datetime-local, select, multiselect, checkbox
    # ou lista, que é um texto separado por vírgulas.
    tipo: str = "text"
    obrigatorio: bool = True
    opcoes: Opcoes | None = None
    na_tabela: bool = True
    so_ao_criar: bool = False
    so_ao_editar: bool = False
    ajuda: str = ""


@dataclass(frozen=True)
class Cadastro:
    rota: str
    titulo: str
    singular: str
    caminho_api: str
    campos: list[Campo]
    so_admin: bool = True
    pode_apagar: bool = True
    aviso: Callable[[list[dict[str, Any]]], list[str]] = field(default=lambda _: [])
    # Monta o corpo da API quando o formulário não é igual a ele, como o período de um bloqueio.
    ajustar: Callable[[dict[str, Any], Request], dict[str, Any]] = field(default=lambda d, _: d)
    # O contrário: prepara um registro da API para a tabela e o formulário.
    exibir: Callable[[dict[str, Any], Request], dict[str, Any]] = field(default=lambda d, _: d)
    filtro_de_busca: bool = False
    # A lista vem limitada, e o registro a editar pode não estar nela.
    le_um_por_vez: bool = False


def _converter(campo: Campo, valores: list[str]) -> Any:
    bruto = valores[0].strip() if valores else ""
    if campo.tipo == "checkbox":
        return bool(valores)
    if campo.tipo == "multiselect":
        return [v for v in valores if v]
    if campo.tipo == "lista":
        return [item.strip() for item in bruto.split(",") if item.strip()]
    if not bruto:
        return None
    if campo.tipo == "number":
        return int(bruto)
    return bruto


def ler_formulario(
    cadastro: Cadastro, formulario: Mapping[str, list[str]], criando: bool
) -> dict[str, Any]:
    corpo: dict[str, Any] = {}
    for campo in cadastro.campos:
        if (campo.so_ao_criar and not criando) or (campo.so_ao_editar and criando):
            continue
        corpo[campo.nome] = _converter(campo, formulario.get(campo.nome, []))
    return corpo


async def opcoes_dos_campos(
    cadastro: Cadastro, request: Request
) -> dict[str, list[tuple[str, str]]]:
    return {c.nome: await c.opcoes(request) for c in cadastro.campos if c.opcoes is not None}
