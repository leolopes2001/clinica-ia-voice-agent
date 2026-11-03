import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from clinica_api.criar_admin import criar
from clinica_core.config import ConfigPostgres, ConfigTokens
from clinica_core.db import criar_engine, criar_fabrica_sessoes
from clinica_core.modelos import Auditoria, Clinica, Usuario
from clinica_painel.config import ConfigPainel

SENHA = "senha-de-teste-123"
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "api" / "alembic.ini"


async def _auditoria(ambiente: Any, acao: str) -> list[Auditoria]:
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        return list(
            await sessao.scalars(
                select(Auditoria).where(
                    Auditoria.clinica_id == ambiente.clinica_id, Auditoria.acao == acao
                )
            )
        )


async def test_sem_login_vai_para_a_tela_de_entrar(anonimo: httpx.AsyncClient) -> None:
    resposta = await anonimo.get("/auditoria")
    assert (resposta.status_code, resposta.headers["location"]) == (303, "/login")


async def test_senha_errada_nao_entra_e_nao_diz_qual_campo_errou(
    anonimo: httpx.AsyncClient, ambiente: Any
) -> None:
    errada = await anonimo.post(
        "/login", data={"email": f"admin-{ambiente.clinica_id}@teste.test", "senha": "errada"}
    )
    desconhecido = await anonimo.post("/login", data={"email": "x@y.test", "senha": "errada"})
    assert errada.status_code == desconhecido.status_code == 401
    assert "E-mail ou senha incorretos" in errada.text
    assert "E-mail ou senha incorretos" in desconhecido.text
    assert "set-cookie" not in errada.headers
    assert len(await _auditoria(ambiente, "login_falhou")) == 1


async def test_cinco_erros_bloqueiam_mesmo_com_a_senha_certa(
    anonimo: httpx.AsyncClient, ambiente: Any
) -> None:
    email = f"admin-{ambiente.clinica_id}@teste.test"
    for _ in range(5):
        await anonimo.post("/login", data={"email": email, "senha": "errada"})
    resposta = await anonimo.post("/login", data={"email": email, "senha": SENHA})
    assert resposta.status_code == 429
    assert "Espere 15 minutos" in resposta.text


async def test_login_grava_auditoria_e_cookie_protegido(
    anonimo: httpx.AsyncClient, ambiente: Any
) -> None:
    resposta = await anonimo.post(
        "/login", data={"email": f"ADMIN-{ambiente.clinica_id}@teste.test", "senha": SENHA}
    )
    assert resposta.status_code == 303
    cookie = resposta.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert len(await _auditoria(ambiente, "login")) == 1


def test_cookie_seguro_e_o_padrao() -> None:
    assert ConfigPainel.model_fields["cookie_seguro"].default is True


async def test_formulario_de_outro_site_e_recusado(admin: httpx.AsyncClient) -> None:
    resposta = await admin.post(
        "/sair", headers={"Origin": "https://outro-site.test", "Host": "painel"}
    )
    assert resposta.status_code == 403


async def test_sair_encerra_a_sessao(admin: httpx.AsyncClient) -> None:
    await admin.post("/sair")
    assert (await admin.get("/auditoria")).status_code == 303


async def test_recepcao_nao_ve_auditoria_nem_usuarios(recepcao: httpx.AsyncClient) -> None:
    for caminho in ("/auditoria", "/cadastros/usuarios"):
        resposta = await recepcao.get(caminho)
        assert resposta.status_code == 403
        assert "Seu perfil não tem acesso" in resposta.text


async def test_menu_da_recepcao_nao_mostra_o_que_e_do_admin(recepcao: httpx.AsyncClient) -> None:
    texto = (await recepcao.get("/minha-senha")).text
    assert "/agenda" in texto
    assert "/auditoria" not in texto


async def test_criar_usuario_nao_guarda_senha_na_auditoria(
    admin: httpx.AsyncClient, ambiente: Any, anonimo: httpx.AsyncClient
) -> None:
    resposta = await admin.post(
        "/cadastros/usuarios",
        data={"email": "Nova@Clinica.test", "perfil": "recepcao", "senha": "senha-bem-longa-1"},
    )
    assert resposta.status_code == 303
    async with ambiente.api.state.fabrica_sessoes() as sessao:
        usuario = await sessao.scalar(select(Usuario).where(Usuario.email == "nova@clinica.test"))
    assert usuario is not None
    assert usuario.senha_hash.startswith("$argon2")
    registro = (await _auditoria(ambiente, "criar"))[0]
    assert registro.usuario_id == ambiente.admin_id
    assert "senha" not in str(registro.depois)
    assert (
        await anonimo.post(
            "/login", data={"email": "nova@clinica.test", "senha": "senha-bem-longa-1"}
        )
    ).status_code == 303


async def test_senha_curta_e_recusada(admin: httpx.AsyncClient) -> None:
    resposta = await admin.post(
        "/cadastros/usuarios", data={"email": "a@b.test", "perfil": "admin", "senha": "curta"}
    )
    assert resposta.status_code == 422
    assert "Confira os campos" in resposta.text


async def test_nao_rebaixa_o_ultimo_admin(admin: httpx.AsyncClient, ambiente: Any) -> None:
    resposta = await admin.post(
        f"/cadastros/usuarios/{ambiente.admin_id}",
        data={
            "email": f"admin-{ambiente.clinica_id}@teste.test",
            "perfil": "recepcao",
            "ativo": "1",
        },
    )
    assert resposta.status_code == 409
    assert "pelo menos um administrador" in resposta.text


async def test_desativar_derruba_a_sessao_na_hora(
    admin: httpx.AsyncClient, recepcao: httpx.AsyncClient, ambiente: Any
) -> None:
    assert (await recepcao.get("/minha-senha")).status_code == 200
    resposta = await admin.post(
        f"/cadastros/usuarios/{ambiente.recepcao_id}",
        data={"email": f"recepcao-{ambiente.clinica_id}@teste.test", "perfil": "recepcao"},
    )
    assert resposta.status_code == 303
    negada = await recepcao.post("/minha-senha", data={"atual": SENHA, "nova": "nova-senha-longa"})
    assert (negada.status_code, negada.headers["location"]) == (303, "/login")


async def test_mudar_perfil_vale_na_hora_apesar_da_sessao(
    admin: httpx.AsyncClient, recepcao: httpx.AsyncClient, ambiente: Any
) -> None:
    assert (await recepcao.get("/auditoria")).status_code == 403
    await admin.post(
        f"/cadastros/usuarios/{ambiente.recepcao_id}",
        data={
            "email": f"recepcao-{ambiente.clinica_id}@teste.test",
            "perfil": "admin",
            "ativo": "1",
        },
    )
    assert (await recepcao.get("/auditoria")).status_code == 200


async def test_trocar_a_propria_senha(recepcao: httpx.AsyncClient) -> None:
    errada = await recepcao.post("/minha-senha", data={"atual": "x", "nova": "nova-senha-longa"})
    assert errada.status_code == 409
    certa = await recepcao.post("/minha-senha", data={"atual": SENHA, "nova": "nova-senha-longa"})
    assert "Senha alterada" in certa.text


async def _api(ambiente: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=ambiente.api), base_url="http://api")


@pytest.mark.parametrize("caminho", ["/clinicas", "/painel/usuarios", "/procedimentos"])
async def test_api_recusa_sem_token(ambiente: Any, caminho: str) -> None:
    async with await _api(ambiente) as api:
        resposta = await api.get(caminho, headers={"X-Clinica-Id": str(ambiente.clinica_id)})
    assert (resposta.status_code, resposta.json()["motivo"]) == (401, "nao_autenticado")


async def test_api_recusa_token_errado(ambiente: Any) -> None:
    async with await _api(ambiente) as api:
        resposta = await api.get(
            "/procedimentos",
            headers={
                "X-Clinica-Id": str(ambiente.clinica_id),
                "Authorization": "Bearer token-errado",
            },
        )
    assert resposta.status_code == 401


async def test_token_dos_servicos_nao_age_como_painel(ambiente: Any) -> None:
    async with await _api(ambiente) as api:
        cabecalhos = {
            **ConfigTokens().cabecalho_servicos,
            "X-Clinica-Id": str(ambiente.clinica_id),
            "X-Usuario-Id": str(ambiente.admin_id),
        }
        for resposta in (
            await api.get("/procedimentos", headers=cabecalhos),
            await api.get("/painel/usuarios", headers=cabecalhos),
        ):
            assert (resposta.status_code, resposta.json()["motivo"]) == (
                403,
                "token_nao_permitido",
            )


async def test_token_do_painel_nao_age_dentro_de_uma_chamada(ambiente: Any) -> None:
    async with await _api(ambiente) as api:
        resposta = await api.get(
            "/procedimentos",
            headers={**ConfigTokens().cabecalho_painel, "X-Chamada-Id": str(uuid4())},
        )
    assert (resposta.status_code, resposta.json()["motivo"]) == (403, "token_nao_permitido")


async def test_token_dos_servicos_abre_chamada(ambiente: Any) -> None:
    async with await _api(ambiente) as api:
        resposta = await api.post(
            "/chamadas",
            headers={
                **ConfigTokens().cabecalho_servicos,
                "X-Clinica-Id": str(ambiente.clinica_id),
                "Idempotency-Key": uuid4().hex,
            },
            json={"telefone": "+5511900000001", "sala": "s"},
        )
    assert resposta.status_code == 201


async def test_usuario_de_outra_clinica_e_recusado(ambiente: Any) -> None:
    async with await _api(ambiente) as api:
        resposta = await api.get(
            "/procedimentos",
            headers={
                **ConfigTokens().cabecalho_painel,
                "X-Clinica-Id": str(uuid4()),
                "X-Usuario-Id": str(ambiente.admin_id),
            },
        )
    assert resposta.status_code == 404


def test_criar_admin_pela_linha_de_comando(banco_vazio: str) -> None:
    command.upgrade(Config(str(ALEMBIC_INI)), "head")
    config = ConfigPostgres(db=banco_vazio)

    async def criar_clinica_e_admin() -> str:
        engine = criar_engine(config)
        async with criar_fabrica_sessoes(engine)() as sessao:
            sessao.add(
                Clinica(nome="C", telefone="+5511", endereco="R", politica_cancelamento_horas=24)
            )
            await sessao.commit()
        await engine.dispose()
        return await criar("Primeiro@Clinica.test", "senha-bem-longa-1", config)

    assert asyncio.run(criar_clinica_e_admin()) == "primeiro@clinica.test"
    with pytest.raises(SystemExit):
        asyncio.run(criar("primeiro@clinica.test", "senha-bem-longa-1", config))
