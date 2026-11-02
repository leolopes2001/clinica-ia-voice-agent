"""Página de teste por voz (T3.4): só para teste interno, com telefones fictícios do seed."""

from importlib.resources import files
from typing import Annotated

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, StringConstraints

from clinica_agent.config import ConfigLiveKit
from clinica_agent.despacho import despachar_chamada_de_teste

PORTA = 8090


class PedidoDeChamada(BaseModel):
    telefone: Annotated[str, StringConstraints(pattern=r"^\+[1-9]\d{7,14}$")]


class ChamadaDeTeste(BaseModel):
    url: str
    token: str
    sala: str


def criar_app(config: ConfigLiveKit | None = None) -> FastAPI:
    livekit = config or ConfigLiveKit()
    app = FastAPI(title="Clínica Voz: teste pelo navegador")

    @app.get("/")
    async def pagina() -> HTMLResponse:
        return HTMLResponse(files("clinica_agent").joinpath("navegador.html").read_text("utf-8"))

    @app.post("/chamada")
    async def chamada(pedido: PedidoDeChamada) -> ChamadaDeTeste:
        sala, token = await despachar_chamada_de_teste(livekit, pedido.telefone)
        return ChamadaDeTeste(url=livekit.url_navegador, token=token, sala=sala)

    return app


if __name__ == "__main__":
    # 0.0.0.0 dentro do contêiner: o Compose só publica a porta em 127.0.0.1.
    uvicorn.run(criar_app(), host="0.0.0.0", port=PORTA)
