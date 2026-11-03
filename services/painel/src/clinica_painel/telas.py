from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def no_fuso(valor: str, fuso: str) -> datetime:
    return datetime.fromisoformat(valor).astimezone(ZoneInfo(fuso))


def data_hora(valor: str | None, fuso: str = "America/Sao_Paulo") -> str:
    return no_fuso(valor, fuso).strftime("%d/%m/%Y %H:%M") if valor else ""


def hora(valor: str, fuso: str = "America/Sao_Paulo") -> str:
    return no_fuso(valor, fuso).strftime("%H:%M")


templates.env.filters["data_hora"] = data_hora
templates.env.filters["hora"] = hora


def pagina(request: Request, nome: str, status: int = HTTPStatus.OK, **dados: Any) -> HTMLResponse:
    return templates.TemplateResponse(
        request, nome, {"sessao": request.session, **dados}, status_code=status
    )
