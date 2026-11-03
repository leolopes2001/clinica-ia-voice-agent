import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from clinica_core.config import ConfigRabbit
from clinica_core.eventos import EventoAgendamento, publicar_eventos
from clinica_core.mensageria import abrir_canal, conectar
from clinica_painel.eventos import FonteDeEventosRabbit


async def test_evento_publicado_chega_a_pagina_aberta_em_menos_de_2_s(vhost: str) -> None:
    clinica = uuid4()
    config = ConfigRabbit()
    fonte = FonteDeEventosRabbit(config)
    fila = fonte.assinar(str(clinica))
    await fonte.abrir()
    conexao = await conectar(config)
    try:
        canal = await abrir_canal(conexao)
        evento = EventoAgendamento(
            tipo="confirmado",
            agendamento_id=uuid4(),
            clinica_id=clinica,
            dentista_id=uuid4(),
            consultorio_id=uuid4(),
            status="confirmado",
            inicio=datetime.now(UTC),
            fim_periodo=datetime.now(UTC),
        )
        # A fila do painel é criada em segundo plano: espera a declaração terminar.
        assert fonte._tarefa is not None
        await asyncio.wait_for(fonte._tarefa, timeout=5)
        await publicar_eventos(canal, [evento])
        assert await asyncio.wait_for(fila.get(), timeout=2) == "confirmado"
    finally:
        await conexao.close()
        await fonte.fechar()
