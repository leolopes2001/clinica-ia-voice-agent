# Spec — Recepcionista de Voz para Clínicas Odontológicas

> **Este arquivo é a fonte da verdade do projeto.** Se algo no código ou em outro documento conflitar com ele, este arquivo vence.

## Como usar este documento

O projeto é uma recepcionista de voz que atende pacientes por **ligação telefônica num chip (SIM)**. Ela tira dúvidas da clínica (RAG) e agenda, remarca e cancela consultas (API via MCP). A administração é feita num painel web, e tudo roda localmente via Docker Compose.

O fluxo segue Spec-Driven Development: **Constituição → Especificação → Plano técnico → Tarefas**. Mudou um requisito? Atualize primeiro a seção correspondente e só depois o código.

**Premissas assumidas** (confirme ou corrija):

- **"Via chip"** = um **gateway GSM/4G com SIM** na rede local, que entrega as ligações via SIP ao LiveKit SIP. Cada chip atende **1 ligação por vez**.
- **"Tudo local"** = todos os serviços rodam na máquina/servidor da clínica. A exceção é o modelo de voz (Gemini Live), que é uma API na nuvem e exige internet.
- **Uma clínica por instalação**, com vários dentistas. O `clinica_id` existe desde o início para não travar multi-clínica no futuro.
- **Idioma:** português do Brasil. **Fuso:** America/Sao_Paulo.

## Constituição

São os princípios que nenhuma tarefa pode violar. Se uma decisão técnica conflitar com eles, a decisão muda.

1. **O banco é a fonte da verdade da agenda.** Nenhum horário duplicado pode existir, mesmo com N ligações simultâneas. A garantia é uma restrição no Postgres, nunca o prompt ou o código do agente.
2. **O agente não toca no banco.** Ele só age por ferramentas MCP. As regras de negócio vivem na API.
3. **O LLM nunca decide identidade nem clínica.** O telefone do chamador e o `clinica_id` vêm do contexto da chamada, não de parâmetros preenchidos pelo modelo.
4. **Ações que mudam a agenda exigem confirmação falada.** O agente repete data, hora, profissional e procedimento, e só executa depois de um "sim" explícito.
5. **Tudo sobe com `docker compose up`.** Nenhum serviço gerenciado além da API do modelo.
6. **Dados de saúde são sensíveis (LGPD).** Coleta mínima, acesso por perfil, auditoria de quem viu e mudou o quê, e retenção definida para áudio e transcrições.
7. **Testes antes do merge.** Cada critério de aceitação vira pelo menos um teste automatizado. O teste de concorrência da agenda é obrigatório.
8. **Python em todo o back-end**, com tipagem (`mypy`), `ruff` e `pytest`.

## Especificação

O paciente liga para o número da clínica e resolve tudo por voz, sem esperar a recepção: tirar dúvidas, agendar, remarcar e cancelar. A clínica administra agenda, profissionais e base de conhecimento num painel web.

### Atores

| Ator | Canal | O que faz |
| --- | --- | --- |
| Paciente | Ligação para o chip | Tira dúvidas, agenda, remarca, cancela |
| Recepção | Painel web | Vê a agenda ao vivo, agenda manualmente, atende transferências |
| Administrador | Painel web | Gerencia dentistas, horários, procedimentos, base de conhecimento, usuários |
| Agente de voz | Interno | Conduz a conversa e chama as ferramentas MCP |

### Escopo

**Dentro:** atendimento de voz receptivo (inbound), dúvidas via RAG, agendar, remarcar, cancelar, identificação por telefone, transferência para humano, painel administrativo, histórico de chamadas.

**Fora (por enquanto):** front-end para paciente, ligações ativas (outbound), pagamentos, prontuário clínico, integração com convênios, WhatsApp. Lembretes por SMS ficam como extensão opcional.

### Histórias de usuário e critérios de aceitação

**H1 — Atender a ligação.** Como paciente, quero ligar e ser atendido na hora.

- [ ] O agente atende em até 2 toques e se apresenta com o nome da clínica.
- [ ] Se o telefone tiver um único paciente cadastrado, o agente cumprimenta pelo primeiro nome.
- [ ] Se o telefone tiver mais de um paciente (por exemplo, pais que marcam para os filhos), o agente cumprimenta sem citar nomes e pergunta para quem é a consulta.
- [ ] Latência entre o fim da fala do paciente e o início da resposta: mediana ≤ 1,5 s.
- [ ] O paciente pode interromper o agente a qualquer momento (barge-in).

**H2 — Tirar dúvidas.** Como paciente, quero perguntar sobre procedimentos, preços, convênios, endereço e preparo.

- [ ] As respostas usam apenas a base de conhecimento da clínica.
- [ ] Sem informação na base, o agente diz que não sabe e oferece transferir. Ele nunca inventa preço ou cobertura.
- [ ] O agente não dá diagnóstico nem orientação clínica. Para dor forte ou urgência, orienta procurar atendimento.

**H3 — Agendar.** Como paciente, quero marcar uma consulta num horário livre.

- [ ] O agente pergunta o procedimento (ou "avaliação") e a preferência de dia, turno e profissional.
- [ ] O agente oferece no máximo 3 horários por vez, falados de forma natural ("quinta, dia 5, às 14h30").
- [ ] O horário escolhido fica **segurado** por 3 minutos enquanto o paciente confirma.
- [ ] Antes de gravar, o agente repete tudo e espera um "sim" explícito.
- [ ] Paciente novo: o agente coleta nome completo e data de nascimento. O telefone vem da ligação.
- [ ] Um mesmo telefone pode ter vários pacientes. Se a consulta for para alguém ainda não cadastrado nesse telefone, o agente faz o cadastro como paciente novo. A mesma pessoa (nome normalizado e data de nascimento) não é cadastrada duas vezes no mesmo telefone.

**H4 — Conflito de horário.** Como clínica, quero que duas pessoas nunca fiquem com o mesmo horário.

- [ ] Com duas ligações simultâneas tentando o mesmo horário do mesmo dentista, exatamente uma consegue segurar. A outra ouve "esse horário acabou de ser ocupado" e recebe alternativas na mesma frase.
- [ ] Um agendamento manual feito no painel concorre com as ligações sob a mesma regra.
- [ ] Um hold expirado libera o horário automaticamente.
- [ ] Repetir a mesma chamada de ferramenta (retry) não cria agendamento duplicado.

**H5 — Remarcar.** Como paciente, quero mudar o horário de uma consulta.

- [ ] O agente identifica os pacientes pelo telefone. Se houver mais de um, pergunta de qual paciente é a consulta.
- [ ] O agente confirma nome e data de nascimento. A verificação vale só para o paciente escolhido, e a chamada registra qual foi.
- [ ] O agente lista as consultas futuras do paciente escolhido e pergunta qual remarcar.
- [ ] O horário antigo só é liberado depois que o novo estiver confirmado (operação atômica).

**H6 — Cancelar.** Como paciente, quero cancelar uma consulta.

- [ ] Exige a mesma identificação da H5 e confirmação falada.
- [ ] O agente informa a política de cancelamento (vinda do RAG) e registra o motivo, se o paciente disser.
- [ ] O horário volta a ficar livre na hora, e o painel atualiza sem recarregar.

**H7 — Falar com humano.** Como paciente, quero ser transferido para a recepção.

- [ ] A transferência acontece quando o paciente pede, quando o agente não entende 2 vezes seguidas ou em caso de reclamação.
- [ ] Fora do horário comercial, o agente avisa e registra um pedido de retorno.

**H8 — Painel: agenda.** Como recepção, quero ver e mexer na agenda em tempo real.

- [ ] Visão diária e semanal por dentista, com status (segurado, confirmado, cancelado, faltou).
- [ ] Criar, remarcar e cancelar pelo painel, com as mesmas regras da API.
- [ ] Mudanças feitas pelo agente aparecem em até 2 s, sem recarregar a página.

**H9 — Painel: cadastros.** Como administrador, quero configurar a clínica.

- [ ] CRUD de dentistas, grade de trabalho semanal, bloqueios (férias, feriados), procedimentos (duração e intervalo de limpeza) e consultórios.
- [ ] Usuários com perfis `admin` e `recepcao`.

**H10 — Painel: base de conhecimento.** Como administrador, quero manter as informações que o agente usa.

- [ ] Upload de PDF, DOCX, MD e TXT, ou edição de textos direto no painel.
- [ ] A indexação roda em segundo plano, com status visível (processando, pronto, erro).
- [ ] Uma tela de teste mostra quais trechos a busca retorna para uma pergunta.

**H11 — Painel: chamadas.** Como administrador, quero auditar os atendimentos.

- [ ] Lista de chamadas com telefone, duração, desfecho (agendou, remarcou, cancelou, dúvida, transferiu, abandonou) e transcrição.
- [ ] As transcrições são apagadas automaticamente após o prazo de retenção configurado.

### Requisitos não funcionais

| Requisito | Meta |
| --- | --- |
| Ligações simultâneas | Igual ao número de chips no gateway (meta inicial: 2) |
| Latência de resposta | Mediana ≤ 1,5 s, p95 ≤ 3 s |
| Busca RAG | p95 ≤ 300 ms (embedding + consulta) |
| Disponibilidade | Serviços com `restart: unless-stopped` e health checks |
| Segurança | Painel com login, senhas com hash argon2, MCP só na rede interna do Docker |
| LGPD | Consentimento falado no início da ligação, auditoria e retenção configurável |

## Plano técnico

São 11 serviços num Docker Compose. O gateway com chip entrega a ligação ao LiveKit pela rede local. O agente ADK conversa pelo Gemini Live e age por dois servidores MCP: um só de leitura (RAG) e outro de agenda, que é um adaptador fino da API. A API é dona das regras, e o Postgres garante que não haja horário duplicado.

```text
Paciente ──► Gateway GSM/4G ──► livekit-sip ──► livekit-server
                                                    │ despacha 1 job por chamada
                                                    ▼
                                 Gemini Live ◄── agent (ADK 2.x)
                                 (nuvem)          │          │
                                                  ▼          ▼
Recepção ──► painel                         mcp-clinica   mcp-conhecimento
               │                                  │          │
               ▼                                  │          │
              api ◄───────────────────────────────┘          │
               │ eventos                                     │
               ▼                                             ▼
worker ◄── RabbitMQ            Postgres 16 (pgvector + exclusion) ◄──┘
   └────────── grava ─────────────────►
```

### Stack

| Camada | Tecnologia | Papel |
| --- | --- | --- |
| Telefonia | Gateway GSM/4G com SIP (1 chip = 1 canal) | Converte a ligação do chip em SIP na rede local |
| Mídia | `livekit-server` + `livekit-sip` (self-hosted) | Salas WebRTC e ponte SIP. Ambos exigem Redis |
| Agente | Python 3.12, `google-adk[livekit]` ≥ 2.9, `livekit-agents` ≥ 1.4 | Conversa e orquestração de ferramentas. Modelo `gemini-live-2.5-flash-native-audio` |
| MCP | SDK oficial `mcp` (FastMCP), transporte Streamable HTTP | `mcp-clinica` (agenda) e `mcp-conhecimento` (RAG) |
| API | FastAPI, SQLAlchemy 2 async, asyncpg, Alembic, Pydantic v2 | Regras de negócio da agenda e dos cadastros |
| Painel | FastAPI + Jinja2 + HTMX + SSE | Administração, todo em Python, sem build de front-end |
| Worker | `aio-pika` | Consumidores RabbitMQ: indexação, expiração de holds, eventos, retenção |
| Dados | PostgreSQL 16 + `pgvector` + `btree_gist` | Agenda, cadastros, vetores, sessões do ADK |
| Broker | RabbitMQ 3 (management) | Eventos de domínio e filas de trabalho |

### Fluxo de uma ligação

1. O paciente liga para o número do chip. O gateway atende e envia um `INVITE` SIP ao `livekit-sip` na rede local (porta 5060).
2. O `livekit-sip` valida o trunk de entrada, cria a sala `call-<uuid>` e, pela dispatch rule, despacha o agente `recepcionista`.
3. O worker do agente abre um job **em processo próprio**. O `LiveKitRunner` liga a sala ao ADK, e o estado da sessão recebe o telefone do chamador (`livekit_caller_phone_number`).
4. O entrypoint registra a chamada na API (`POST /chamadas`), recebe um `chamada_id` e o injeta nos headers das conexões MCP (`X-Chamada-Id`, `X-Clinica-Id`).
5. O agente pede consentimento LGPD, cumprimenta e conduz a conversa chamando as ferramentas MCP.
6. Ao desligar, o agente publica `chamada.encerrada` com desfecho e transcrição, e o worker grava tudo.

### Concorrência

Há dois problemas diferentes, com duas soluções diferentes.

**A) Várias ligações ao mesmo tempo.** Cada ligação tem sala, job e processo próprios, então não há estado compartilhado na memória do agente. A API e os MCPs são assíncronos e sem estado, com pool de conexões ao Postgres. As sessões do ADK ficam no Postgres (`DatabaseSessionService`). O limite real é físico: **um chip atende uma ligação por vez**. Para 2 pacientes simultâneos são necessários 2 chips num gateway de 2 portas. Com todos os canais ocupados, o chamador recebe sinal de ocupado da operadora.

**B) Duas pessoas querendo o mesmo horário.** O Postgres recusa a sobreposição:

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE agendamentos (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinica_id      uuid NOT NULL REFERENCES clinicas(id),
  dentista_id     uuid NOT NULL REFERENCES dentistas(id),
  consultorio_id  uuid NOT NULL REFERENCES consultorios(id),
  paciente_id     uuid REFERENCES pacientes(id),  -- nulo enquanto segurado
  procedimento_id uuid NOT NULL REFERENCES procedimentos(id),
  periodo         tstzrange NOT NULL,              -- [inicio, fim + intervalo_limpeza)
  status          text NOT NULL CHECK (status IN
                  ('segurado','confirmado','cancelado','expirado','faltou','concluido')),
  expira_em       timestamptz,                     -- só para 'segurado'
  chamada_id      uuid REFERENCES chamadas(id),    -- quem segurou
  origem          text NOT NULL CHECK (origem IN ('voz','painel')),
  motivo_cancelamento text,
  criado_em       timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT segurado_exige_expiracao CHECK (status <> 'segurado' OR expira_em IS NOT NULL),
  CONSTRAINT confirmado_exige_paciente CHECK (status <> 'confirmado' OR paciente_id IS NOT NULL),
  CONSTRAINT voz_exige_chamada CHECK (origem <> 'voz' OR chamada_id IS NOT NULL),
  CONSTRAINT sem_conflito_dentista EXCLUDE USING gist
    (dentista_id WITH =, periodo WITH &&)
    WHERE (status IN ('segurado','confirmado')),
  CONSTRAINT sem_conflito_consultorio EXCLUDE USING gist
    (consultorio_id WITH =, periodo WITH &&)
    WHERE (status IN ('segurado','confirmado'))
);
```

Regras de cada operação:

- **Segurar:** primeiro valida o que a constraint não conhece (grade, bloqueios, habilitação e antecedência); um horário inválido vira `{ok: false, motivo: "indisponivel", alternativas: [...]}`. Depois, numa transação, toma os locks, marca como `expirado` os holds vencidos daquele dentista ou daquele consultório (`status='segurado' AND expira_em < now()`), porque os dois entram nas exclusion constraints, e insere com `status='segurado'` e `expira_em = now() + duração do hold`. Erro `23P01` (exclusion violation) vira a resposta `{ok: false, motivo: "ocupado", alternativas: [...]}`. As alternativas são todos os candidatos do mesmo dentista, do dia pedido até 7 dias depois; a escolha das 3 opções fica na T2.1.
- **Locks:** segurar, confirmar e remarcar tomam `pg_advisory_xact_lock` por dentista e por consultório, sempre em ordem fixa. Inserções simultâneas no mesmo horário podem dar deadlock (`40P01`) na exclusion constraint, em vez de `23P01`: numa medição com 50 conexões, isso ocorreu em 9 de 60 rodadas sem lock e em nenhuma de 100 com lock. Os locks só evitam esse deadlock: quem garante que não há duplicidade continua sendo a constraint.
- **Expirar:** depois do commit, cada hold novo publica na fila de atraso uma mensagem com `expiration` igual à duração do hold (`AGENDA_DURACAO_HOLD_MIN`). Como todos os holds têm a mesma duração, a ordem FIFO garante que expirem na ordem certa, e a duração muda sem recriar a fila; ao mudá-la, as mensagens já na fila ainda seguram as seguintes até vencerem. Quando ela vence, cai via dead-letter na fila `holds.expirar`, e o worker marca `expirado` se o hold ainda estiver segurado e vencido. Mensagem inválida ou erro ao processar vai para `holds.expirar.dlq`, sem voltar à fila. A publicação não bloqueia o agendamento: tem limite de 2 s e, se falhar, só gera um log de aviso, porque o horário é liberado de qualquer forma quando o hold vence. Além disso, a cada 60 s uma varredura do worker marca como `expirado` todos os holds vencidos, o que cobre mensagens perdidas e o caso de o worker atrasar, junto com a limpeza feita ao segurar.
- **Confirmar:** `UPDATE ... SET status='confirmado', paciente_id=:paciente, expira_em=NULL WHERE id=:hold AND clinica_id=:clinica AND status='segurado' AND expira_em > now() AND origem=:origem AND chamada_id IS NOT DISTINCT FROM :chamada`. Assim, cada ligação só confirma os próprios holds, e o painel só confirma holds do painel. Se nenhuma linha mudar, o hold foi perdido e o agente oferece novas opções. Na ligação, o paciente vem de `chamadas.paciente_id` e nunca do corpo da requisição; no painel, vem do corpo e precisa ser da mesma clínica.
- **Remarcar:** o novo horário já está segurado. Numa única transação, confirma o novo e cancela o antigo. Só vale para consultas `confirmado` que ainda não começaram; na ligação, exige identidade verificada e consulta do paciente selecionado. **Limitação conhecida do MVP:** não dá para remarcar para um horário que se sobrepõe à própria consulta (de 14h–15h para 14h30), porque o novo hold colide com a consulta antiga e a resposta é `ocupado`.
- **Cancelar:** tem as mesmas condições de remarcar e é sempre permitido, mesmo fora do prazo da política. A resposta informa `dentro_do_prazo` e `politica_cancelamento_horas`.
- **Idempotência:** toda operação de escrita recebe `Idempotency-Key` (hash de `chamada_id` + ferramenta + argumentos). A API guarda só as respostas de sucesso na tabela `idempotencia` por 24 h e devolve a mesma resposta num retry; uma falha não muda nada e pode ser executada de novo. Um retry de segurar só devolve a resposta guardada se o hold ainda estiver válido. Caso contrário, executa de novo, para o paciente não ficar preso a um hold vencido quando pede o mesmo horário outra vez.
- **Eventos:** toda mudança gravada publica um evento `agendamento.*` depois do commit: segurar gera `criado`, confirmar gera `confirmado`, remarcar gera um único `remarcado`, com a consulta cancelada em `anterior`, e cancelar gera `cancelado`. Cada hold vencido gera `expirado` uma única vez, venha da fila, da varredura ou da limpeza ao segurar, inclusive quando esse novo hold termina em `ocupado`. Um retry que devolve resposta guardada não publica nada. O evento leva só ids, status e horário, sem nenhum dado do paciente. A publicação segue a mesma regra da expiração: tem limite de 2 s e, se falhar, só gera um log de aviso. Por isso, o evento é só um aviso: o painel relê a agenda do banco ao recebê-lo e ao reconectar.
- **Painel:** usa os mesmos endpoints, com `origem='painel'`, e cai na mesma restrição. A API identifica a origem pelos headers: com `X-Chamada-Id`, usa a clínica da chamada e origem `voz`; sem ele, usa `X-Clinica-Id` e origem `painel`.

Exemplo — duas ligações pedindo quinta às 14h ao mesmo tempo:

```text
Paciente A   Agente A        API + Postgres        Agente B   Paciente B
   │ "14h?" ──►│                   │                  │◄── "14h?" │
   │           │── segurar 14h ───►│◄── segurar 14h ──│           │
   │           │                   │ (aceita só uma)  │           │
   │           │◄── ok, 3 min ─────│── ocupado + ────►│           │
   │           │                   │   14h30, 15h     │           │
   │◄ confirma?│                   │                  │ 14h30? ──►│
   │ "sim" ───►│── confirmar ─────►│◄─ segurar+conf ──│◄─ "sim" ──│
   A fica com 14h                nenhum duplicado        B fica com 14h30
```

### Modelo de dados

Todas as tabelas, exceto `clinicas` e `idempotencia`, têm `clinica_id` com FK para `clinicas`.

| Tabela | Campos principais | Observação |
| --- | --- | --- |
| `clinicas` | nome, telefone, endereco, fuso, politica_cancelamento_horas | 1 linha no MVP |
| `dentistas` | nome, especialidades[], ativo | |
| `consultorios` | nome, ativo | Recurso físico com restrição própria |
| `procedimentos` | nome, sinonimos[], duracao_min, intervalo_limpeza_min, dentistas_habilitados[] | `sinonimos` ajuda o agente a reconhecer "limpeza" = "profilaxia". `dentistas_habilitados` vazio significa que ninguém está habilitado |
| `grade_trabalho` | dentista_id, dia_semana, inicio, fim, consultorio_id | Grade semanal recorrente |
| `bloqueios` | dentista_id (nulo = clínica toda), periodo, motivo | Férias, feriados, almoço |
| `pacientes` | nome, nome_normalizado, telefone_e164, data_nascimento, consentimento_em | Um telefone pode ter vários pacientes. Índice em (clinica_id, telefone_e164) e unicidade em (clinica_id, telefone_e164, nome_normalizado, data_nascimento). `nome_normalizado` é gerado no banco: sem acentos, em minúsculas e com espaços compactados |
| `agendamentos` | (ver SQL acima) | |
| `chamadas` | telefone, sala, inicio, fim, desfecho, transcricao, apagar_em, paciente_id, identidade_verificada_em, tentativas_identidade | `paciente_id` é o paciente escolhido na chamada. Trocar de paciente desfaz a verificação, mas não zera as tentativas |
| `pedidos_retorno` | protocolo (único), chamada_id, telefone, motivo, status, criado_em | Pedidos de retorno da H7 |
| `conhecimento_documentos` | titulo, arquivo, status, ativo | |
| `conhecimento_trechos` | documento_id, ordem, texto, embedding `vector(768)`, tsv `tsvector` | Índice HNSW + GIN. `tsv` é gerado no banco a partir de `texto` |
| `idempotencia` | chave, resposta_json, criado_em | Limpeza após 24 h |
| `usuarios` / `auditoria` | email, senha_hash, perfil / usuario_id, acao, entidade, antes, depois | |

As configurações do agente (T5.6) ganham tabela numa migration própria, nessa tarefa.

Horários livres são calculados, não armazenados: grade de trabalho, menos bloqueios, menos agendamentos ativos do dentista ou do consultório. Um hold vencido não ocupa o horário, mesmo antes de o worker marcá-lo como `expirado`. Os candidatos começam sempre em múltiplos de 15 minutos (:00, :15, :30, :45, no fuso da clínica) dentro de cada intervalo livre, e só entram se a duração mais o intervalo de limpeza couberem inteiros. O cálculo descarta os horários que começam antes de agora + `antecedencia_minima_min` (padrão: 120 minutos) e devolve todos os candidatos, em ordem cronológica.

### Contratos MCP

Nenhuma ferramenta recebe telefone, `paciente_id` da sessão ou `clinica_id` como parâmetro. O servidor resolve tudo pelo header `X-Chamada-Id`. A única exceção é `selecionar_paciente`, que recebe um id da lista devolvida por `identificar_chamador`: o servidor recusa qualquer id que não pertença ao telefone da chamada.

**`mcp-clinica`** (escrita, chama a API):

| Ferramenta | Entrada | Saída |
| --- | --- | --- |
| `identificar_chamador` | — | lista de pacientes do telefone `{paciente_id, primeiro_nome}`, vazia se não houver. Com um único paciente, ele já fica selecionado na chamada |
| `selecionar_paciente` | paciente_id | paciente selecionado na chamada. Desfaz qualquer verificação anterior |
| `cadastrar_paciente` | nome, data_nascimento | paciente, que fica selecionado na chamada |
| `verificar_identidade` | data_nascimento | `ok` true/false para o paciente selecionado (3 tentativas por chamada) |
| `listar_procedimentos` | — | nome, duração, sinônimos |
| `listar_horarios_livres` | procedimento, a_partir_de, ate, dentista?, turno? | até 3 opções variadas com texto falado pronto |
| `segurar_horario` | procedimento, dentista, inicio | `hold_id` + `expira_em`, ou `ocupado` + alternativas |
| `confirmar_agendamento` | hold_id | agendamento confirmado ou `hold_perdido` |
| `listar_meus_agendamentos` | — | consultas futuras do paciente selecionado (exige identidade verificada) |
| `remarcar_agendamento` | agendamento_id, hold_id | agendamento novo |
| `cancelar_agendamento` | agendamento_id, motivo? | ok + aviso de política |
| `registrar_pedido_retorno` | motivo | protocolo |

**`mcp-conhecimento`** (somente leitura):

| Ferramenta | Entrada | Saída |
| --- | --- | --- |
| `buscar_conhecimento` | pergunta, k = 3 | trechos `{texto, fonte}`, cada um com até 800 caracteres |

O `turno` vale pelo horário de início da consulta, no fuso da clínica: manhã `[início, 12h)`, tarde `[12h, 18h)` e noite `[18h, fim)`. Uma consulta às 18h00 é da noite.

A transferência e o desligamento usam o `LiveKitToolset` do ADK, não o MCP. Os headers por chamada usam o mecanismo de headers dinâmicos do `McpToolset`. Se a versão do ADK não tiver, a alternativa é um wrapper de ferramenta que adiciona o `chamada_id`.

### RabbitMQ

Há um exchange `clinica.eventos` do tipo topic, mais o `clinica.dlx` (direct) para dead-letters. Cada fila durável tem a sua DLQ (`<fila>.dlq`), com duas exceções: o dead-letter de `holds.atraso` é a própria `holds.expirar`, e a fila do painel não tem DLQ.

| Routing key / fila | Produtor | Consumidor | Para quê |
| --- | --- | --- | --- |
| `agendamento.*` (criado, confirmado, remarcado, cancelado, expirado) → fila exclusiva e auto-delete, declarada por cada processo do painel ao subir | API e worker | Painel (SSE) | Atualizar a agenda ao vivo. O worker produz `agendamento.expirado` ao expirar pela fila ou pela varredura; a API, na limpeza ao segurar. Sem painel no ar, os eventos são descartados; com várias instâncias, todas recebem |
| `holds.atraso` (expiração por mensagem) → DLX → `holds.expirar` | API | Worker | Liberar holds vencidos. Uma varredura do worker a cada 60 s, fora do broker, cobre mensagens perdidas |
| `conhecimento.indexar` | Painel | Worker | Extrair, fatiar, gerar embeddings e gravar |
| `chamada.encerrada` | Agente | Worker | Gravar transcrição, desfecho e métricas |
| `manutencao.diaria` | Agendador do worker | Worker | Retenção LGPD e limpeza de idempotência |

O Redis continua no Compose só porque o LiveKit exige. O broker da aplicação é o RabbitMQ.

### RAG

- **Ingestão:** o arquivo é salvo num volume e o worker recebe `conhecimento.indexar`. Ele extrai o texto (`pypdf`, `python-docx`) e fatia em trechos de cerca de 500 tokens, com 15% de sobreposição, respeitando títulos. Depois gera o embedding (padrão: `gemini-embedding-001` com 768 dimensões) e grava vetor + `tsvector('portuguese')`.
- **Busca híbrida:** top 20 por vetor (cosseno) e top 20 por full-text, combinados por Reciprocal Rank Fusion. Só os 3 primeiros trechos são devolvidos, filtrados por clínica e documento ativo.
- **Qualidade:** o painel tem uma tela "testar pergunta". Um conjunto fixo de 30 perguntas com a resposta esperada roda no CI.

### Painel administrativo

O painel é servido pelo FastAPI com templates Jinja2 e HTMX, e recebe atualizações ao vivo por SSE (alimentado pela fila de eventos). As páginas são: Agenda (dia/semana por dentista), Pacientes, Dentistas e grade, Procedimentos, Consultórios, Bloqueios, Base de conhecimento, Chamadas, Usuários e Configurações do agente (saudação, voz, horário comercial, número de transferência, antecedência mínima). O login usa sessão com cookie seguro, e toda escrita gera um registro de auditoria.

### Docker Compose (local)

| Serviço | Imagem / build | Portas expostas no host | Depende de |
| --- | --- | --- | --- |
| `postgres` | `pgvector/pgvector:pg16` | 5432 (só dev) | — |
| `rabbitmq` | `rabbitmq:3-management` | 15672 (UI), 5672 (só dev) | — |
| `redis` | `redis:7` | — | — |
| `livekit` | `livekit/livekit-server` | 7880, 7881/tcp, faixa UDP de RTC | redis |
| `livekit-sip` | `livekit/sip` | 5060/udp, 10000–20000/udp (só na LAN) | livekit, redis |
| `api` | build `./services/api` | `127.0.0.1:${API_PORT:-8000}` | postgres, rabbitmq |
| `painel` | build `./services/painel` | 8080 | api, rabbitmq |
| `mcp-clinica` | build `./services/mcp_clinica` | — (rede interna) | api |
| `mcp-conhecimento` | build `./services/mcp_conhecimento` | — (rede interna) | postgres |
| `agent` | build `./services/agent` | — | livekit, MCPs |
| `worker` | build `./services/worker` | — | postgres, rabbitmq |

Como o gateway está na mesma rede local, as portas SIP não precisam ser abertas para a internet. Trunk de entrada e dispatch rule ficam no Redis e são recriados pelo script `scripts/bootstrap_sip.sh` (`lk sip inbound create` + `lk sip dispatch create`). Para desenvolver sem gateway, um softphone (Linphone, Zoiper) liga direto para o IP do `livekit-sip`.

### Estrutura do repositório

```text
clinica-voz/
├── docker-compose.yml
├── .env.example
├── docs/                 # spec.md (este documento)
├── infra/                # livekit.yaml, sip.yaml
├── scripts/              # bootstrap_sip.sh, seed.py
├── packages/
│   └── core/             # modelos, schemas Pydantic, cliente RabbitMQ, config
└── services/
    ├── api/              # FastAPI + Alembic
    ├── painel/           # FastAPI + Jinja2 + HTMX
    ├── mcp_clinica/
    ├── mcp_conhecimento/
    ├── agent/            # ADK + LiveKitRunner, prompts/
    └── worker/
```

O monorepo é gerenciado com `uv` workspaces, com um `pyproject.toml` por serviço.

## Tarefas

São 7 fases, cada uma terminando em algo que roda e pode ser testado. As fases 1 e 2 não dependem de telefonia, então dá para começar antes de comprar o gateway.

**Fase 0 — Fundação**

- [x] T0.1 Monorepo com `uv` workspaces, `ruff`, `mypy`, `pytest`, pre-commit.
- [x] T0.2 `docker-compose.yml` com postgres, rabbitmq e redis, mais health checks. *Pronto quando:* `docker compose up` sobe tudo saudável.
- [x] T0.3 `packages/core`: settings (`pydantic-settings`), conexão async ao banco e cliente RabbitMQ com declaração de exchanges e filas.

**Fase 1 — Agenda e concorrência (o núcleo)**

- [x] T1.1 Migrations Alembic de todas as tabelas, com `btree_gist`, `pgvector`, `unaccent` e as duas exclusion constraints.
- [x] T1.2 Cálculo de horários livres (grade − bloqueios − agendamentos), com testes de borda: almoço, feriado, procedimento que não cabe no fim do turno.
- [x] T1.3 Endpoints segurar, confirmar, remarcar e cancelar, com idempotência e tradução do erro `23P01`, a rota HTTP de listar horários livres e o serviço `api` no Compose.
- [x] T1.4 Fila de atraso + DLX para expiração de holds, e o consumidor no worker. Trocar o TTL da fila `holds.atraso` por expiração por mensagem (`expiration`), com a duração vinda das settings (`AGENDA_DURACAO_HOLD_MIN`): como todos os holds têm a mesma duração, a ordem FIFO garante que expirem na ordem certa, e a duração muda sem recriar a fila. Ao concluir, atualizar a seção RabbitMQ e a regra de expirar. Incluir uma migration com um CHECK em `agendamentos`: `origem='voz'` exige `chamada_id`.
- [x] T1.5 **Teste de concorrência:** 50 tarefas `asyncio` tentando o mesmo horário. *Pronto quando:* exatamente 1 sucesso e 49 respostas `ocupado`, em 100 execuções seguidas.
- [x] T1.6 Publicação dos eventos `agendamento.*`. Quando o consumidor de `holds.expirar` ou a varredura do worker expirarem um hold, isso também gera `agendamento.expirado`, para o painel atualizar; nesse caso, o produtor é o worker. A limpeza de holds vencidos ao segurar também marca `expirado`; nesse caso, o produtor é a API.

**Fase 2 — Servidores MCP e RAG**

- [x] T2.1 `POST /chamadas` e os endpoints de identidade na API (identificar, selecionar, cadastrar e verificar), e `mcp-clinica` com as 12 ferramentas, resolvendo identidade pelo `X-Chamada-Id`. Em `listar_horarios_livres`, filtrar por `turno` e depois escolher as até 3 opções variadas entre os candidatos.
- [x] T2.2 Pipeline de ingestão no worker (extração, chunking, embeddings, gravação).
- [ ] T2.3 `mcp-conhecimento` com busca híbrida e RRF.
- [ ] T2.4 Conjunto de 30 perguntas de avaliação. *Pronto quando:* o trecho certo aparece no top 3 em ≥ 90% delas.
- [ ] T2.5 Testar os dois MCPs com o MCP Inspector e com um agente ADK em modo texto (`adk web`).

**Fase 3 — Agente de voz (sem telefone)**

- [ ] T3.1 Subir `livekit` local e o agente com `LiveKitRunner`, `McpToolset` x2 e `LiveKitToolset`.
- [ ] T3.2 Prompt do sistema em `agent/prompts/`: persona, consentimento, regra de confirmação, limites clínicos, como falar datas e horários.
- [ ] T3.3 `DatabaseSessionService` no Postgres, com usuário e schema próprios para as sessões do ADK.
- [ ] T3.4 Testar pelo navegador com o LiveKit Agents Playground ou o `agent-starter-react` (só para teste interno). Criar o `scripts/seed.py`, com dados fictícios de exemplo para o teste por voz: clínica, 2 dentistas, procedimentos com sinônimos, grade semanal, um bloqueio de almoço e alguns pacientes com telefones fictícios. Telefones reais ficam só no `.env`, nunca no seed. *Pronto quando:* agendar, remarcar e cancelar funcionam por voz.
- [ ] T3.5 Medir latência (meta: mediana ≤ 1,5 s).

**Fase 4 — Telefonia pelo chip**

- [ ] T4.1 `livekit-sip` no Compose, mais o `bootstrap_sip.sh` (trunk de entrada restrito ao IP do gateway e dispatch rule para `recepcionista`).
- [ ] T4.2 Teste com softphone na LAN.
- [ ] T4.3 Configurar o gateway: rota GSM → IP para o `livekit-sip`, codec G.711 e caller ID repassado.
- [ ] T4.4 Teste real com 2 celulares ligando ao mesmo tempo para o mesmo horário. *Pronto quando:* o critério da H4 passa.
- [ ] T4.5 Transferência para a recepção (SIP REFER ou discagem para ramal/celular).

**Fase 5 — Painel administrativo**

- [ ] T5.1 Login, perfis e auditoria, mais a autenticação entre o painel e a API, que até lá confia nos headers.
- [ ] T5.2 Agenda dia/semana com SSE ao vivo, e criar/remarcar/cancelar manual.
- [ ] T5.3 Cadastros: dentistas, grade, bloqueios, procedimentos, consultórios. O painel avisa quando um procedimento está sem dentista habilitado. Uma migration nova adiciona a `grade_trabalho` exclusion constraints que impedem duas linhas sobrepostas do mesmo dentista no mesmo dia da semana, e o mesmo para o consultório.
- [ ] T5.4 Base de conhecimento: upload, status de indexação e tela "testar pergunta".
- [ ] T5.5 Chamadas: lista, transcrição e desfecho.
- [ ] T5.6 Configurações do agente, incluindo `antecedencia_minima_min`, que até lá vem das settings (`AGENDA_ANTECEDENCIA_MINIMA_MIN`).

**Fase 6 — Produção local e LGPD**

- [ ] T6.1 Retenção automática de transcrições e texto de consentimento revisado. A retenção só pode apagar a transcrição de uma chamada ligada a uma consulta de voz: a FK e o CHECK `voz_exige_chamada` impedem apagar a chamada ou anular `chamada_id`. *A resolver:* o prazo do telefone guardado nessa linha.
- [ ] T6.2 Backup diário do Postgres (`pg_dump` num volume e cópia externa).
- [ ] T6.3 Logs estruturados e um painel simples de saúde (serviços, filas, chamadas ativas).
- [ ] T6.4 Runbook: o que fazer se cair a internet, o gateway ou a API do Gemini.
- [ ] T6.5 Trocar as senhas padrão de desenvolvimento do Compose (Postgres e RabbitMQ) antes de usar em produção.

## Decisões em aberto e riscos

| Item | Opções | Padrão assumido |
| --- | --- | --- |
| Quantos chips (canais simultâneos) | 1, 2, 4 ou 8 portas | 2 chips |
| Gateway de chip | Gateway SIP dedicado com 4G/VoLTE ou modem USB + Asterisk (`chan_dongle`/`chan_quectel`) | Gateway dedicado: o caminho via modem USB é mais frágil |
| Modelo de embedding | `gemini-embedding-001` (API) ou modelo local multilíngue | Gemini. O local deixa o RAG 100% offline, com custo de CPU/RAM |
| Duração do hold | 2 a 5 min | 3 min |
| Retenção de transcrições | 30, 90 ou 180 dias | 90 dias |
| Gravação do áudio | Não gravar, ou gravar via LiveKit Egress | Não gravar no MVP |
| Transferência para humano | Ramal SIP na recepção ou celular via outro chip | A definir com a clínica |

**Riscos**

- **O runner LiveKit do ADK é experimental.** Mitigação: a Fase 3 é uma prova de conceito antes de investir no resto. O plano B é usar o `livekit-agents` puro com o plugin Google e chamar os mesmos MCPs, sem mudar API nem banco.
- **Dependência de internet:** sem internet não há Gemini Live, e a ligação precisa cair numa mensagem gravada ou ser desviada para a recepção. Isso está previsto no runbook (T6.4).
- **Rede móvel:** as operadoras vêm desligando 2G/3G. Compre um gateway com 4G e VoLTE e confirme a compatibilidade com a operadora do chip.
- **Qualidade de áudio do celular (8 kHz):** números e nomes podem ser mal entendidos. Mitigação: o agente sempre repete datas, horários e nomes antes de confirmar (Constituição, item 4).
- **Uso do chip pela operadora:** algumas operadoras restringem o uso de chips pré-pagos em gateways. Verifique o contrato e prefira um plano empresarial.

## Decisões tomadas pelo agente

Decisões técnicas tomadas durante a implementação, com a opção mais conservadora.

| Tarefa | Decisão | Por quê |
| --- | --- | --- |
| T1.6 | A limpeza de holds vencidos ao segurar usa `RETURNING` e publica `agendamento.expirado` pela API, depois do commit, mesmo quando o novo hold termina em `ocupado`. | Essa limpeza é gravada mesmo quando o novo hold não entra, e o `UPDATE` só muda holds ainda `segurado`. Assim, cada hold gera um único evento, seja qual for o produtor. |
| T1.6 | Os eventos são publicados depois do commit, sem outbox. Se a publicação falhar ou o processo cair nesse intervalo, o evento se perde e fica só o log de aviso. Um retry não publica de novo. | Segue a regra já usada na expiração: o broker nunca derruba o agendamento, e o banco é a fonte da verdade. Na T5.2, o painel precisa reler a agenda ao receber um evento e ao reconectar o SSE, sem depender da ordem nem da entrega de todos os eventos. |
| T1.6 | O evento leva tipo, ids da clínica, do agendamento, do dentista e do consultório, status e horário. Não leva paciente nem motivo de cancelamento. | Coleta mínima (LGPD): o painel busca o resto na API, sob o login dele. |
| T1.6 | Remarcar gera um único `agendamento.remarcado`, com a consulta cancelada no campo `anterior`, e não um `cancelado` separado. | A remarcação é uma operação atômica, e o painel fica sabendo dos dois horários afetados por um único evento. |
| T1.6 | Ao segurar, a mensagem da fila de atraso e os eventos são publicados em paralelo, para que o limite de 2 s não some duas vezes na resposta. Eventos de um mesmo lote dividem um único limite de 2 s. | Mantém a promessa de que a publicação não atrasa o agendamento além do limite. Numa varredura muito grande, o que não couber no limite se perde, com log de aviso. |
| T2.1 | O servidor usa `MCPServer`, do `mcp` 2.x, em modo sem estado (`stateless_http`, `json_response`). | O `mcp` 2.x renomeou `FastMCP` para `MCPServer`, e o ADK aceita `mcp<3`. O servidor continua sem estado, como pede a seção de concorrência. |
| T2.1 | A escolha das 3 opções, o texto falado e a busca de procedimento e dentista por nome ficam na API (`clinica_api/voz.py`, rotas `/opcoes-horarios` e `/holds/por-nome`). O `/horarios-livres` e o `/holds` continuam iguais. | As regras de negócio ficam na API, e o `mcp-clinica` continua um adaptador fino. O contrato MCP recebe nomes, e a API trabalha com ids. |
| T2.1 | Procedimento: comparação exata com nome ou sinônimo, sem acento nem caixa. Dentista: todas as palavras ditas precisam estar no nome, ignorando "Dr.", "Dra." e "doutora". Mais de um resultado vira `procedimento_ambiguo` ou `dentista_ambiguo`, sem chute. | É mais seguro o agente perguntar de novo do que segurar horário com o profissional errado. |
| T2.1 | As 3 opções: a primeira é a mais cedo; as seguintes maximizam a novidade de dia, turno e dentista e, depois, a distância, de até 1 h, das já escolhidas. Empates ficam com a mais cedo. | "Variadas" não estava definido, e a regra é determinística e testável. |
| T2.1 | Só os erros de data de nascimento gastam as 3 tentativas. Um erro depois de verificar desfaz a verificação. | Uma mãe ou um pai que verifica vários filhos na mesma ligação não fica bloqueado, e a adivinhação continua limitada a 3 erros. |
| T2.1 | Selecionar, cadastrar, identificar e verificar não usam `Idempotency-Key`. | Guardar a resposta quebraria uma sequência legítima, como selecionar A, depois B, depois A de novo. Selecionar, cadastrar e identificar já dão o mesmo resultado se repetidos, e repetir uma data errada só gasta tentativa. |
| T2.1 | `cadastrar_paciente` não marca a identidade como verificada. Para remarcar ou cancelar, o paciente recém-cadastrado também passa por `verificar_identidade`. | É a regra mais restritiva: na H5, a verificação vem sempre antes de listar consultas. |
| T2.1 | `POST /chamadas` só aceita telefone em E.164 e exige `Idempotency-Key` (o agente usará a sala como chave). | Uma ligação repetida no retry não duplica a chamada. Normalizar o caller ID do gateway e tratar número oculto fica para as fases 3 e 4. |
| T2.1 | O protocolo do pedido de retorno tem 8 dígitos aleatórios. | O agente precisa falar o protocolo em voz alta. |
| T2.1 | Na falha de `segurar_horario`, o MCP devolve em `alternativas` só as até 3 opções faladas. A rota `/holds` continua devolvendo todos os candidatos. | Reduz o que o modelo precisa ler durante a ligação. |
| T2.1 | `pacientes.consentimento_em` continua nulo no cadastro por voz, porque ainda não há ferramenta nem fluxo que registrem o consentimento. | Não marcar um consentimento que ninguém coletou. O registro precisa ser definido junto com o prompt da T3.2. |
| T2.2 | A contagem de tokens é uma estimativa: cerca de 4 caracteres por token, ou seja, trechos de até 2000 caracteres e sobreposição de até 300, em frases inteiras. | "Cerca de 500 tokens" não pede precisão, e isso dispensa um tokenizador. |
| T2.2 | Um título nunca fica no meio de um trecho, e cada trecho começa com o caminho de títulos ("Preços > Limpeza"). Seções curtas não são juntadas. | Melhora a precisão da busca: o trecho leva o contexto do título. |
| T2.2 | Os embeddings são gerados antes da transação. Na mesma transação, o worker apaga os trechos antigos e grava os novos. Se a indexação falhar, os trechos antigos ficam valendo e o documento fica com status `erro`. | A busca nunca vê um documento pela metade, e o banco não fica preso a uma chamada de rede. |
| T2.2 | Falha ao indexar vai para `conhecimento.indexar.dlq`, sem voltar à fila, depois das tentativas do próprio cliente do Gemini (até 5). | Segue a regra usada em `holds.expirar`. |
| T2.2 | A indexação usa um canal próprio, com `prefetch=1`. | Um documento por vez respeita o limite de requisições do Gemini e não atrasa a expiração de holds. |
| T2.2 | Sem `GOOGLE_API_KEY`, o worker sobe normalmente e só a indexação falha. | A expiração de holds não pode depender da chave. |
| T2.2 | A indexação só lê arquivos. Um documento com `arquivo` nulo fica com status `erro`. | A T5.4 precisa salvar como arquivo no volume o texto editado no painel, porque a tabela não tem coluna de texto. |
