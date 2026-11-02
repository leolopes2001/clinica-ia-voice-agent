# CLAUDE.md — Recepcionista de Voz para Clínicas Odontológicas

## O que é este projeto

Agente de voz que atende pacientes por ligação telefônica (chip SIM → gateway GSM/4G → LiveKit SIP).
Ele tira dúvidas (RAG via MCP) e agenda, remarca e cancela consultas (API via MCP).
A clínica administra tudo por um painel web. Tudo roda localmente com `docker compose up`.

## Fonte da verdade

A especificação completa está em `docs/spec.md`. **Leia antes de qualquer tarefa.**
Ela contém, nesta ordem: Constituição → Especificação (histórias H1–H11 com critérios de aceitação)
→ Plano técnico → Tarefas (T0.1 … T6.5) → Decisões em aberto.

Se algo no código conflitar com a spec, a spec vence. Se a spec estiver errada ou incompleta,
**pare e pergunte** antes de mudar o comportamento. Não invente requisitos.

## Regras inegociáveis (resumo da Constituição)

1. Nenhum horário duplicado. A garantia é a exclusion constraint no Postgres, nunca o prompt do agente.
2. O agente de voz nunca acessa o banco. Ele só usa ferramentas MCP. As regras de negócio ficam na API.
3. Telefone do chamador e `clinica_id` vêm do contexto da chamada (header `X-Chamada-Id`), nunca de parâmetros preenchidos pelo LLM.
4. Ações que mudam a agenda exigem confirmação falada explícita.
5. Tudo sobe com `docker compose up`. Única dependência externa: API do Gemini.
6. Dados de saúde são sensíveis (LGPD): coleta mínima, auditoria, retenção configurável.
7. Todo critério de aceitação vira pelo menos um teste. O teste de concorrência (T1.5) é obrigatório.
8. Python 3.12 em todo o back-end, com tipagem (`mypy`), `ruff` e `pytest`.

## Stack

- Agente: `google-adk[livekit]` >= 2.9 (LiveKitRunner, experimental), `livekit-agents` >= 1.4, modelo `gemini-live-2.5-flash-native-audio`
- MCP: SDK oficial `mcp` (FastMCP), transporte Streamable HTTP — `mcp-clinica` e `mcp-conhecimento`
- API e painel: FastAPI, SQLAlchemy 2 async, asyncpg, Alembic, Pydantic v2, Jinja2 + HTMX + SSE
- Dados: PostgreSQL 16 + pgvector + btree_gist
- Broker: RabbitMQ (`aio-pika`). O Redis existe só porque o LiveKit exige.
- Monorepo com `uv` workspaces

## Como trabalhar

- Faça **uma tarefa por vez** (ex.: "T1.3"), na ordem das fases. Não pule fases.
- Antes de codar, apresente um plano curto da tarefa e espere aprovação.
- Escreva os testes junto com o código. A tarefa só termina quando os testes passam e o critério "Pronto quando" da spec é atendido.
- Cada tarefa tem a sua branch, criada a partir da `main` (ex.: `t1.3-endpoints-agenda`).
- Ao terminar, marque o checkbox da tarefa em `docs/spec.md` (`- [ ]` → `- [x]`) e faça um commit com a mensagem `T1.3: <resumo>`.
- Quando os testes passarem e o critério "Pronto quando" for atendido, faça o merge da branch na `main` sem pedir confirmação.
- A `main` é a branch principal. Depois de cada merge nela, faça o push para a `origin/main` sem pedir confirmação.
- APIs de bibliotecas que mudam rápido (ADK, LiveKit, MCP): consulte a documentação atual antes de usar. Não confie na memória.
  - LiveKit: use o MCP `livekit-docs`, se estiver configurado.
  - ADK: https://adk.dev

## Comandos

```bash
docker compose up -d            # sobe a infraestrutura
uv sync                         # instala dependências
uv run pytest                   # testes
uv run ruff check . && uv run mypy .
uv run alembic upgrade head     # migrations (dentro de services/api)
```
