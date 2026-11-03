"""Tela de saúde: serviços, filas, chamadas ativas e backup. Nada aqui chama o Gemini."""

import asyncio
import json
import logging
from http import HTTPStatus
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response

from clinica_core.config import ConfigRabbit, ConfigTokens
from clinica_painel.api import chamar
from clinica_painel.config import ConfigPainel
from clinica_painel.telas import pagina

logger = logging.getLogger(__name__)

router = APIRouter()

_LIMITE_S = 2.0
# Sem consumidor nestas filas, o worker não está fazendo o trabalho dele.
_FILAS_DO_WORKER = ("holds.expirar", "conhecimento.indexar", "chamada.encerrada")


async def _responde(sondas: httpx.AsyncClient, url: str) -> bool:
    try:
        resposta = await sondas.get(url, timeout=_LIMITE_S)
    except httpx.HTTPError:
        return False
    return resposta.status_code < HTTPStatus.INTERNAL_SERVER_ERROR


async def _servicos(sondas: httpx.AsyncClient, config: ConfigPainel) -> list[dict[str, Any]]:
    alvos = {
        "API": f"{config.api_url}/saude",
        "mcp-clinica": f"{config.mcp_clinica_url}/saude",
        "mcp-conhecimento": f"{config.mcp_conhecimento_url}/saude",
        "LiveKit": config.livekit_url,
        "Agente de voz": config.agente_url,
    }
    resultados = await asyncio.gather(*(_responde(sondas, url) for url in alvos.values()))
    return [{"nome": n, "ok": ok} for n, ok in zip(alvos, resultados, strict=True)]


async def _filas(sondas: httpx.AsyncClient, config: ConfigPainel) -> list[dict[str, Any]] | None:
    rabbit = ConfigRabbit()
    try:
        resposta = await sondas.get(
            f"{config.rabbit_gerencia_url}/api/queues",
            auth=(rabbit.user, rabbit.password.get_secret_value()),
            timeout=_LIMITE_S,
        )
        resposta.raise_for_status()
    except httpx.HTTPError as erro:
        logger.warning("RabbitMQ não respondeu à tela de saúde: %r", erro)
        return None
    filas = [
        {
            "nome": fila["name"],
            "mensagens": fila.get("messages", 0),
            "consumidores": fila.get("consumers", 0),
            "dlq": fila["name"].endswith(".dlq"),
        }
        for fila in resposta.json()
        # As filas do painel são uma por processo e nascem vazias.
        if not fila["name"].startswith("painel.")
    ]
    return sorted(filas, key=lambda f: f["nome"])


def _ler_backup(config: ConfigPainel) -> dict[str, Any] | None:
    try:
        lido: dict[str, Any] = json.loads(
            (config.backups_dir / "ultimo.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    return lido


def _alertas(
    config: ConfigPainel,
    servicos: list[dict[str, Any]],
    filas: list[dict[str, Any]] | None,
    backup: dict[str, Any] | None,
    banco_ok: bool,
) -> list[str]:
    alertas: list[str] = []
    if not banco_ok:
        alertas.append("A API não consegue falar com o banco.")
    alertas += [f"{s['nome']} não responde." for s in servicos if not s["ok"]]
    if filas is None:
        alertas.append("O RabbitMQ não responde: sem isso, a agenda não atualiza ao vivo.")
    else:
        for fila in filas:
            if fila["dlq"] and fila["mensagens"]:
                alertas.append(f"{fila['nome']} tem {fila['mensagens']} mensagens com falha.")
            if fila["nome"] in _FILAS_DO_WORKER and not fila["consumidores"]:
                alertas.append(f"O worker não está consumindo {fila['nome']}.")
    if backup is None:
        alertas.append("Nenhum backup registrado.")
    else:
        if backup.get("situacao") != "ok":
            alertas.append("O último backup falhou.")
        if backup.get("externo") == "sem_senha":
            alertas.append("O backup não tem cópia externa: defina BACKUP_SENHA.")
        elif backup.get("externo") == "falhou":
            alertas.append("A cópia externa do último backup falhou.")
    padrao_tokens = ConfigTokens.model_fields["painel"].default
    padrao_sessao = ConfigPainel.model_fields["segredo_sessao"].default
    if ConfigTokens().painel == padrao_tokens or config.segredo_sessao == padrao_sessao:
        alertas.append("Há senhas de desenvolvimento em uso: rode scripts/gerar_segredos.py.")
    return alertas


@router.get("/saude-do-sistema")
async def ver(request: Request) -> Response:
    # Permissão primeiro: só o administrador dispara as sondas.
    banco = await chamar(request, "GET", "/painel/saude")
    config: ConfigPainel = request.app.state.config
    sondas: httpx.AsyncClient = request.app.state.sondas
    servicos, filas = await asyncio.gather(_servicos(sondas, config), _filas(sondas, config))
    backup = await asyncio.to_thread(_ler_backup, config)
    return pagina(
        request,
        "saude_sistema.html",
        servicos=servicos,
        filas=filas,
        backup=backup,
        chamadas_ativas=banco["chamadas_ativas"],
        alertas=_alertas(config, servicos, filas, backup, banco["banco"] == "ok"),
    )
