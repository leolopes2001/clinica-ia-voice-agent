# Como testar a Fase 2

Roteiro para testar os dois MCPs e a base de conhecimento na própria máquina. Tudo usa os dados fictícios de `scripts/seed.py`.

Os comandos de `export` valem só para o terminal em que você os rodou. Rode tudo no mesmo terminal e na raiz do repositório, ou repita os `export` ao abrir outro. Se o seu `API_PORT` não for 8001, troque a porta nos comandos.

## 1. Subir os serviços e carregar os dados

```bash
docker compose up -d --build --wait
uv sync --all-packages
uv run python scripts/seed.py
```

O seed imprime `CLINICA_ID=...` e pede a indexação da base. Guarde o id:

```bash
export AGENTE_TEXTO_CLINICA_ID=<id impresso>
```

Para conferir a indexação, todos os documentos devem estar com status `pronto`:

```bash
docker compose exec postgres psql -U clinica -d clinica -c "select titulo, status from conhecimento_documentos"
```

## 2. Rodar os MCPs fora do Docker

No Compose, os MCPs ficam só na rede interna, sem porta no host. Para testar, rode cada um num terminal próprio:

```bash
MCP_CLINICA_API_URL=http://localhost:8001 MCP_CLINICA_PORTA=8101 uv run python -m clinica_mcp_clinica
```

```bash
MCP_CONHECIMENTO_PORTA=8102 uv run python -m clinica_mcp_conhecimento
```

## 3. MCP Inspector

O Inspector 2.x exige Node 22.19 ou mais recente, tanto na interface web quanto na linha de comando. A interface é opcional. Crie uma chamada de teste, usando um telefone do seed:

```bash
export CHAMADA=$(curl -s -X POST localhost:8001/chamadas \
  -H "Authorization: Bearer token-dos-servicos-de-desenvolvimento" \
  -H "X-Clinica-Id: $AGENTE_TEXTO_CLINICA_ID" \
  -H "Idempotency-Key: teste-$(date +%s)" \
  -H "Content-Type: application/json" \
  -d '{"telefone":"+5511900000001","sala":"inspector"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["chamada_id"])')
echo "$CHAMADA"
```

Abra a interface com `npx @modelcontextprotocol/inspector`. Conecte em `http://localhost:8101/mcp` e depois em `http://localhost:8102/mcp`, com transporte Streamable HTTP e o header `X-Chamada-Id` igual ao valor de `$CHAMADA`.

Ou use a linha de comando:

```bash
npx @modelcontextprotocol/inspector --cli --transport http \
  --server-url http://localhost:8102/mcp --header "X-Chamada-Id: $CHAMADA" \
  --method tools/call --tool-name buscar_conhecimento --tool-arg "pergunta=Quanto custa a limpeza?"
```

## 4. Agente em modo texto (`adk web`)

```bash
AGENTE_TEXTO_API_URL=http://localhost:8001 uv run adk web services/agent/adk_texto --port 8100
```

Abra http://localhost:8100 e escolha `recepcionista_texto`. Cada sessão abre uma chamada com o telefone `+5511900000001` (Maria Oliveira, nascida em 12/03/1985). Para testar um telefone com dois pacientes, rode com `AGENTE_TEXTO_TELEFONE=+5511900000002` (Carla, nascida em 25/07/1980, e Bruno, nascido em 03/11/2014).

Sugestões de conversa:

- "Quanto custa a limpeza? Vocês aceitam o Sorriso Pleno?"
- "Vocês fazem implante capilar?" O agente deve dizer que não sabe e oferecer a transferência.
- "Quero marcar uma limpeza na próxima semana, de tarde."
- "Quero remarcar minha consulta" e "Quero cancelar minha consulta". Os dois pedem a data de nascimento.

Este agente usa um prompt mínimo, só para exercitar as ferramentas. O prompt da recepcionista vem na T3.2.

## 5. Testes automáticos

```bash
uv run pytest
uv run pytest tests/test_avaliacao_rag.py -s   # acerto e latência da busca, com o Gemini real
```
