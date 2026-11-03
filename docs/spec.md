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
- [ ] Em toda ligação, logo depois de se apresentar e antes de consultar qualquer dado do chamador, o agente pede o consentimento (LGPD), e a chamada registra a resposta.
- [ ] Se a pessoa recusar, o agente só tira dúvidas e transfere: não identifica, não cadastra, não agenda, não remarca, não cancela e não registra pedido de retorno. A transcrição não é guardada.
- [ ] Sem resposta ao pedido de consentimento (por exemplo, a pessoa desligou antes), o tratamento é o mesmo da recusa.
- [ ] Com número oculto, o agente só tira dúvidas e transfere, como na recusa. A transcrição segue a resposta dada ao consentimento.
- [ ] Depois do consentimento, se o telefone tiver um único paciente cadastrado, o agente cumprimenta pelo primeiro nome.
- [ ] Depois do consentimento, se o telefone tiver mais de um paciente (por exemplo, pais que marcam para os filhos), o agente cumprimenta sem citar nomes e pergunta para quem é a consulta.
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
- [ ] Antes de gravar, o agente repete tudo e espera um "sim" explícito. Confirmar segue a mesma trava da H5: só passa se a última fala transcrita do paciente, feita depois de segurar o horário e de qualquer cadastro ou seleção de paciente, contiver uma confirmação explícita.
- [ ] Paciente novo: o agente coleta nome completo e data de nascimento. O telefone vem da ligação.
- [ ] Agendar e cadastrar exigem o consentimento dado na chamada. O cadastro por voz grava `pacientes.consentimento_em`.
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
- [ ] Remarcar tem duas etapas, preparar e executar. Executar só é permitido se a última fala transcrita do paciente, feita depois de preparar, contiver uma confirmação explícita. "Essa mesma" e "pode ser" não contam. "Sim, não tem problema", "não tem problema" e "sem problema" contam.

**H6 — Cancelar.** Como paciente, quero cancelar uma consulta.

- [ ] Exige a mesma identificação da H5 e confirmação falada, com as mesmas duas etapas e a mesma trava da H5.
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
| Busca RAG | p95 ≤ 800 ms (embedding + consulta), com cache em memória dos embeddings das perguntas. Reavaliar na T3.5 |
| Disponibilidade | Serviços com `restart: unless-stopped` e health checks |
| Segurança | Painel com login, senhas com hash argon2, MCP só na rede interna do Docker |
| LGPD | Consentimento falado no início de toda ligação, gravado na chamada; auditoria e retenção configurável |

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
| Agente | Python 3.12, `google-adk[livekit]` ≥ 2.9, `livekit-agents` ≥ 1.4 | Conversa e orquestração de ferramentas. O modelo vem de `AGENTE_MODELO_VOZ`, com chave do AI Studio. Candidatos: `gemini-3.1-flash-live-preview` e `gemini-2.5-flash-native-audio-latest` (escolha na T3.1) |
| MCP | SDK oficial `mcp` 2.x (`MCPServer`, antigo FastMCP), transporte Streamable HTTP | `mcp-clinica` (agenda) e `mcp-conhecimento` (RAG) |
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
5. O agente se apresenta, pede o consentimento LGPD e registra a resposta na chamada. Com consentimento, cumprimenta e conduz a conversa chamando as ferramentas MCP. Sem consentimento, só tira dúvidas e transfere.
6. Ao desligar, o agente publica `chamada.encerrada` com desfecho e transcrição, e o worker grava tudo. Sem consentimento dado na chamada, a transcrição é descartada.

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
| `chamadas` | telefone, sala, inicio, fim, desfecho, transcricao, apagar_em, paciente_id, identidade_verificada_em, tentativas_identidade, consentimento_em, consentimento_recusado_em | `paciente_id` é o paciente escolhido na chamada. Trocar de paciente desfaz a verificação, mas não zera as tentativas. As colunas de consentimento entram na T3.2. O `telefone` passa a aceitar nulo (número oculto) na T4.6 |
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
| `registrar_consentimento` | aceitou | resposta gravada na chamada (entra na T3.2). Sem consentimento dado, a API recusa as ferramentas de identidade e as que mudam a agenda, com o motivo `consentimento_ausente` |
| `selecionar_paciente` | paciente_id | paciente selecionado na chamada. Desfaz qualquer verificação anterior |
| `cadastrar_paciente` | nome, data_nascimento | paciente, que fica selecionado na chamada |
| `verificar_identidade` | data_nascimento | `ok` true/false para o paciente selecionado (3 tentativas por chamada) |
| `listar_procedimentos` | — | nome, duração, sinônimos |
| `listar_horarios_livres` | procedimento, a_partir_de, ate, dentista?, turno? | até 3 opções variadas com texto falado pronto |
| `segurar_horario` | procedimento, dentista, inicio | `hold_id` + `expira_em`, ou `ocupado` + alternativas |
| `confirmar_agendamento` | hold_id | agendamento confirmado, `hold_perdido`, ou `confirmacao_ausente` / `preparo_ausente` pela trava do agente |
| `listar_meus_agendamentos` | — | consultas futuras do paciente selecionado (exige identidade verificada) |
| `preparar_remarcacao` | agendamento_id, hold_id | fala do horário atual e do novo, sem mudar nada |
| `executar_remarcacao` | agendamento_id, hold_id | agendamento novo, ou `confirmacao_ausente` / `preparo_ausente` pela trava do agente |
| `preparar_cancelamento` | agendamento_id | fala da consulta e prazo da política, sem mudar nada |
| `executar_cancelamento` | agendamento_id, motivo? | ok + aviso de política, ou `confirmacao_ausente` / `preparo_ausente` pela trava do agente |
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
| `agendamento.*` (criado, confirmado, remarcado, cancelado, expirado, atualizado) → fila exclusiva e auto-delete, declarada por cada processo do painel ao subir | API e worker | Painel (SSE) | Atualizar a agenda ao vivo. O worker produz `agendamento.expirado` ao expirar pela fila ou pela varredura; a API, na limpeza ao segurar. Sem painel no ar, os eventos são descartados; com várias instâncias, todas recebem |
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
| `painel` | build `./services/painel` | `${PAINEL_BIND:-127.0.0.1}:${PAINEL_PORTA:-8080}` | api, rabbitmq |
| `mcp-clinica` | build `./services/mcp_clinica` | — (rede interna) | api |
| `mcp-conhecimento` | build `./services/mcp_conhecimento` | — (rede interna) | api, postgres |
| `agent` | build `./services/agent` | — | livekit, MCPs |
| `worker` | build `./services/worker` | — | postgres, rabbitmq |

Como o gateway está na mesma rede local, as portas SIP não precisam ser abertas para a internet. Trunk de entrada e dispatch rule ficam no Redis e são recriados pelo script `scripts/bootstrap_sip.sh` (`lk sip inbound create` + `lk sip dispatch create`). Para desenvolver sem gateway, um softphone (Linphone, Zoiper) liga direto para o IP do `livekit-sip`.

### Estrutura do repositório

```text
clinica-voz/
├── docker-compose.yml
├── .env.example
├── docs/                 # spec.md (este documento), roteiros de teste, validacao-final.md, runbook.md
├── exemplos/             # base de conhecimento da clínica fictícia
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
- [x] T2.3 `mcp-conhecimento` com busca híbrida e RRF.
- [x] T2.4 Conjunto de 30 perguntas de avaliação. *Pronto quando:* o trecho certo aparece no top 3 em ≥ 90% delas.
- [x] T2.5 Testar os dois MCPs com o MCP Inspector e com um agente ADK em modo texto (`adk web`).

**Fase 3 — Agente de voz (sem telefone)**

- [x] T3.1 Subir `livekit` local e o agente com `LiveKitRunner`, `McpToolset` x2 e `LiveKitToolset`. O nome do modelo vem de `AGENTE_MODELO_VOZ`. Testar `gemini-3.1-flash-live-preview` e `gemini-2.5-flash-native-audio-latest`, com chave do AI Studio, e escolher pela latência e pela qualidade do português.
- [x] T3.2 Prompt do sistema em `agent/prompts/`: persona, consentimento, regra de confirmação, limites clínicos, como falar datas e horários. Consentimento: uma migration adiciona `consentimento_em` e `consentimento_recusado_em` a `chamadas`, e o `mcp-clinica` ganha `registrar_consentimento`, sua 13ª ferramenta. A API, e não o prompt, recusa com `consentimento_ausente` as ferramentas de identidade, `segurar_horario`, `confirmar_agendamento`, `listar_meus_agendamentos`, `remarcar_agendamento`, `cancelar_agendamento` e `registrar_pedido_retorno`. O cadastro por voz grava `pacientes.consentimento_em`, e o `chamada.encerrada` descarta a transcrição quando não houve consentimento. O prompt também manda dizer que não sabe quando os trechos da busca não trouxerem a resposta (H2).
- [x] T3.3 `DatabaseSessionService` no Postgres, com usuário e schema próprios para as sessões do ADK.
- [x] T3.4 Testar pelo navegador com o LiveKit Agents Playground ou o `agent-starter-react` (só para teste interno). Criar o `scripts/seed.py`, com dados fictícios de exemplo para o teste por voz: clínica, 2 dentistas, procedimentos com sinônimos, grade semanal, um bloqueio de almoço e alguns pacientes com telefones fictícios. Telefones reais ficam só no `.env`, nunca no seed. *Pronto quando:* agendar, remarcar e cancelar funcionam por voz.
- [x] T3.5 Medir latência (meta: mediana ≤ 1,5 s). Reavaliar a meta da busca RAG (p95 ≤ 800 ms) com o cache de embeddings em uso real.
- [x] T3.6 Trava de confirmação abaixo do modelo: remarcar e cancelar em duas etapas (preparar → executar), e um `before_tool_callback` no agente que só permite executar com confirmação explícita na última fala transcrita do paciente, feita depois de preparar. A mesma trava vale para `confirmar_agendamento`, em que segurar o horário faz o papel de preparar. *Pronto quando:* "Essa mesma" não cancela, e "Sim, pode cancelar" cancela, por voz.
- [ ] T3.7 Frase curta de espera ("só um instante, vou verificar") antes de chamar as ferramentas que demoram, mantendo o fim de fala padrão, e nova medição de latência, separando o primeiro som da resposta de verdade.
- [x] T3.8 GitHub Actions com `ruff`, `mypy` e `pytest`, com Postgres e RabbitMQ como serviços, a cada push e pull request.

**Fase 4 — Telefonia pelo chip**

- [ ] T4.1 `livekit-sip` no Compose, mais o `bootstrap_sip.sh` (trunk de entrada restrito ao IP do gateway e dispatch rule para `recepcionista`).
- [ ] T4.2 Teste com softphone na LAN.
- [ ] T4.3 Configurar o gateway: rota GSM → IP para o `livekit-sip`, codec G.711 e caller ID repassado.
- [ ] T4.4 Teste real com 2 celulares ligando ao mesmo tempo para o mesmo horário. *Pronto quando:* o critério da H4 passa.
- [ ] T4.5 Transferência para a recepção (SIP REFER ou discagem para ramal/celular).
- [ ] T4.6 Número oculto: a chamada é aceita sem telefone (`chamadas.telefone` passa a aceitar nulo, e o `POST /chamadas` deixa de exigir o campo). A API recusa, com o motivo `telefone_oculto`, as mesmas ferramentas bloqueadas pela falta de consentimento, e o agente só tira dúvidas e transfere. Sem telefone não há pedido de retorno: fora do horário comercial, o agente avisa e pede para ligar de novo. Normalizar para E.164 o caller ID recebido do gateway.

**Fase 5 — Painel administrativo**

- [x] T5.1 Login, perfis e auditoria, mais a autenticação entre o painel e a API, que até lá confia nos headers.
- [x] T5.2 Agenda dia/semana com SSE ao vivo, e criar/remarcar/cancelar manual.
- [x] T5.3 Cadastros: dentistas, grade, bloqueios, procedimentos, consultórios. O painel avisa quando um procedimento está sem dentista habilitado. Uma migration nova adiciona a `grade_trabalho` exclusion constraints que impedem duas linhas sobrepostas do mesmo dentista no mesmo dia da semana, e o mesmo para o consultório.
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
- **Latência da busca RAG:** em 02/11/2026, antes do cache, a busca completa mediu mediana de 467 ms e p95 de 764 ms, e quase todo o tempo é do embedding no Gemini. A meta passou a ser p95 ≤ 800 ms, com pouca folga numa pergunta nova; o cache só acelera perguntas repetidas. Com o cache, as mesmas 30 perguntas mediram p95 de 630 ms na primeira vez e de 3 ms ao repetir. Reavaliar na T3.5. Se não bastar, a alternativa é um modelo de embedding local, que continua como decisão em aberto.
- **Latência da conversa (T3.5, em 02/11/2026):** a meta de mediana ≤ 1,5 s só é atingida em turnos sem ferramenta. Com voz sintetizada, na própria máquina e sem o caminho do telefone, a resposta sem ferramenta mediu mediana de 1,50 s e p95 de 1,68 s, e a resposta depois de uma busca, mediana de 3,32 s e p95 de 3,42 s. Com 500 ms de silêncio de fim de fala (`AGENTE_SILENCIO_FIM_DE_FALA_MS`), a resposta depois de uma busca caiu para 2,89 s e 3,08 s. A meta continua como está, e isso **ainda é uma decisão em aberto**. Numa busca real, a ferramenta mediu mediana de 0,67 a 0,74 s e p95 de 1,04 a 1,49 s, acima da meta de 800 ms. Essa medida vem do cliente de teste, entre o pedido e a resposta da ferramenta: as outras ferramentas MCP medem de 0,03 a 0,10 s no mesmo caminho. A abertura levou de 2 a 3 s, com alguns casos de cerca de 7 s, sem causa identificada. A interrupção (barge-in) parou o agente 0,57 s depois de a fala começar.
- **Frase de espera (T3.7, em 02/11/2026, ainda aberta):** pedir a frase no prompt não adiantou. Em 13 turnos, o primeiro som depois de uma busca ficou em mediana de 3,55 s e p95 de 4,32 s, sem melhora sobre os 3,32 s de antes. Na maioria dos turnos, o modelo chama a ferramenta primeiro e fala "só um instante" já junto com a resposta. A frase saiu do prompt. Agora o agente toca a frase por conta própria, ao iniciar a ferramenta, sem depender do modelo, mas isso ainda não foi medido nem ouvido. A medição foi interrompida porque os créditos do Gemini acabaram, e a reavaliação da meta continua pendente.
- **Créditos do Gemini:** com os créditos pré-pagos esgotados, o Gemini fecha a conexão (`1011`), e a ligação cai sem o paciente ouvir nada. O runbook (T6.4) precisa cobrir esse caso, junto com a queda da internet, e vale configurar um alerta de saldo no AI Studio.
- **Modelo de voz:** os dois candidatos são de pré-lançamento ou apontam para a versão mais recente (`-latest`) e podem mudar sem aviso. Por isso o nome fica em `AGENTE_MODELO_VOZ`, e não no código.
- **Uso do chip pela operadora:** algumas operadoras restringem o uso de chips pré-pagos em gateways. Verifique o contrato e prefira um plano empresarial.

## Pendente de validação

O que o código já cobre com fakes, mas só pode ser confirmado com crédito do Gemini, com o gateway ou por uma pessoa. O roteiro passo a passo está em `docs/validacao-final.md`.

### Com crédito do Gemini

- [ ] Rodar a avaliação do RAG com embeddings reais: `RODAR_TESTES_GEMINI=1 uv run pytest tests/test_avaliacao_rag.py`.
- [ ] Trava em `confirmar_agendamento` por voz: "essa mesma" não agenda, e "sim, não tem problema" agenda. Paciente novo: o "sim" ao nome não confirma a consulta. Os testes usam fakes da transcrição.
- [ ] Gerar o áudio da frase de espera: `uv run python scripts/gerar_frase_espera.py --motor gemini --voz <voz do atendimento>`. Conferir que a voz combina com a do atendimento e refazer se a voz mudar no painel.
- [ ] T3.7: medir de novo a latência com a frase, separando o primeiro som da resposta de verdade, e decidir a meta de 1,5 s depois de uma ferramenta, que segue em aberto. Conferir se a frase não atropela a resposta e se o corte com a fala do paciente funciona.

### Com o gateway e a telefonia

### Pelo usuário
- [ ] Decidir como a recepção acessa o painel: na própria máquina do servidor, ou pela rede com `PAINEL_BIND` e HTTPS na frente. Com HTTP puro na rede, a senha trafega aberta.
- [ ] Na sua máquina, a porta 8080 está ocupada por um processo Java, que não é do projeto. Defina `PAINEL_PORTA=8088` no `.env`, ou libere a 8080, antes de subir o painel.

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
| T2.1 | `pacientes.consentimento_em` continua nulo no cadastro por voz até a T3.2, que cria o registro do consentimento. | Não marcar um consentimento que ninguém coletou. |
| T2.2 | A contagem de tokens é uma estimativa: cerca de 4 caracteres por token, ou seja, trechos de até 2000 caracteres e sobreposição de até 300, em frases inteiras. | "Cerca de 500 tokens" não pede precisão, e isso dispensa um tokenizador. |
| T2.2 | Um título nunca fica no meio de um trecho, e cada trecho começa com o caminho de títulos ("Preços > Limpeza"). Seções curtas não são juntadas. | Melhora a precisão da busca: o trecho leva o contexto do título. |
| T2.2 | Os embeddings são gerados antes da transação. Na mesma transação, o worker apaga os trechos antigos e grava os novos. Se a indexação falhar, os trechos antigos ficam valendo e o documento fica com status `erro`. | A busca nunca vê um documento pela metade, e o banco não fica preso a uma chamada de rede. |
| T2.2 | Falha ao indexar vai para `conhecimento.indexar.dlq`, sem voltar à fila, depois das tentativas do próprio cliente do Gemini (até 5). | Segue a regra usada em `holds.expirar`. |
| T2.2 | A indexação usa um canal próprio, com `prefetch=1`. | Um documento por vez respeita o limite de requisições do Gemini e não atrasa a expiração de holds. |
| T2.2 | Sem `GOOGLE_API_KEY`, o worker sobe normalmente e só a indexação falha. | A expiração de holds não pode depender da chave. |
| T2.2 | A indexação só lê arquivos. Um documento com `arquivo` nulo fica com status `erro`. | A T5.4 precisa salvar como arquivo no volume o texto editado no painel, porque a tabela não tem coluna de texto. |
| T2.3 | A spec pede trechos de cerca de 500 tokens na indexação (até 2000 caracteres) e no máximo 800 caracteres na resposta. Quando o trecho passa de 800, a resposta mantém o título e recorta em torno das frases com mais palavras da pergunta. | Respeita os dois números sem cortar às cegas a parte que responde à pergunta. |
| T2.3 | A busca por texto casa com qualquer palavra da pergunta (`plainto_tsquery` com `&` trocado por `\|`), e `k` fica limitado a 3. | Uma pergunta falada quase nunca tem todas as palavras no mesmo trecho, e a spec devolve só os 3 primeiros. |
| T2.3 | Se o embedding da pergunta falhar, a busca segue só por texto, com limite de 2 s e 2 tentativas. | O paciente está na linha: é melhor responder com menos precisão do que deixá-lo esperando. |
| T2.3 | Não há nota de corte de relevância: a busca devolve os 3 melhores, mesmo fracos. | A spec não define corte. Quem decide se o trecho responde é o agente, e o prompt da T3.2 precisa mandá-lo dizer que não sabe quando o trecho não trouxer a resposta (H2). |
| T2.3 | O servidor abre as transações em modo somente leitura, e a clínica vem sempre de `chamadas.clinica_id`, ignorando `X-Clinica-Id`. | Garante no banco o "somente leitura" do contrato, e o LLM não influi na clínica. |
| T2.4 | A base da clínica fictícia fica em `exemplos/conhecimento/*.md`, e as 30 perguntas em `tests/avaliacao/perguntas.json`. Uma pergunta só conta como acerto se o documento certo vier no top 3 e a resposta esperada estiver no texto devolvido, já recortado em 800 caracteres. | É mais rígido que o critério da spec e mede o que o agente de fato recebe. |
| T2.4 | A avaliação usa os embeddings reais e é ignorada sem `GOOGLE_API_KEY`. No CI (T3.8), a chave precisa estar nos segredos do repositório. | O resultado com embeddings falsos não mediria nada. |
| T2.5 | O `scripts/seed.py` foi antecipado da T3.4, porque o teste dos MCPs precisa de dados. Ele já cobre o que a T3.4 pede e também indexa a base de exemplo. | Evita um seed temporário só para a Fase 2. O almoço vira um bloqueio por dia útil nos próximos 90 dias, porque `bloqueios` não é recorrente, e rodar o seed de novo estende a janela. |
| T2.5 | O agente de texto (`services/agent/adk_texto`) usa `header_provider` do `McpToolset` para enviar o `X-Chamada-Id`, com um prompt mínimo. O modelo padrão é `gemini-flash-latest`, porque o `gemini-2.5-flash` responde 404 para esta chave. | Valida na prática o mecanismo de headers dinâmicos que a Fase 3 vai usar. |
| T2.5 | O `mcp-conhecimento` também depende de `api` saudável no Compose. | As tabelas vêm das migrations, aplicadas pela API ao subir. É o mesmo motivo do worker. |
| T2.5 | O teste com o MCP Inspector foi feito pela linha de comando. A interface web ficou de fora, por decisão do usuário. | A linha de comando cobre listar e chamar as ferramentas, com o header da chamada. |
| Ajuste | O cache dos embeddings das perguntas fica na memória do `mcp-conhecimento`, com até 1024 perguntas. A chave é o hash da pergunta normalizada (sem diferença de caixa e de espaços), e falhas não entram no cache. | O texto da pergunta pode ter dado de saúde: o hash evita mantê-lo na memória, e o cache some ao reiniciar. Com 768 dimensões, ocupa cerca de 25 MB no pior caso. |
| T3.1 | Modelo de voz: `gemini-3.1-flash-live-preview`. Em três conversas iguais por voz sintetizada, o `gemini-2.5-flash-native-audio-latest` chamou ferramentas sem os argumentos, em sequência, trocou palavras por lixo na fala ("ódioestado") e travou conversas. O 3.1 completou todas, com mediana de 2,4 a 2,8 s nas perguntas. | A escolha foi por estabilidade e pelas transcrições. A qualidade do português aos ouvidos fica para o teste do usuário. O nome continua em `AGENTE_MODELO_VOZ`. |
| T3.1 | O agente fica na rede do contêiner `livekit` (`network_mode: service:livekit`), e o LiveKit anuncia a mídia em `127.0.0.1`, numa única porta UDP (7882). Todas as portas ficam em `127.0.0.1`. | Com um só endereço anunciado, navegador e agente alcançam a mídia sem depender do IP da rede local. A Fase 4 revê o `LIVEKIT_NODE_IP`. |
| T3.1 | `NO_GCE_CHECK=true` na imagem do agente. | O `McpToolset` do ADK procura credenciais do Google Cloud e esperava cerca de 12 s pelo servidor de metadados em cada ligação. A abertura caiu de 14 s para cerca de 2 s. |
| T3.1 | A abertura vem de um evento de usuário gravado na sessão antes de o modelo conectar. | O modelo só fala depois de uma fala do usuário, e o `LiveKitRunner` não expõe a fila. O ADK já envia o histórico ao conectar. |
| T3.1 | `GET /clinicas` na API, com id e nome. O agente usa a única clínica da instalação, ou `AGENTE_CLINICA_ID` se configurado. | O agente precisa do nome para se apresentar e não toca no banco. Evita um passo manual de copiar o id depois do seed. |
| T3.1 | Sem SIP, o telefone vem dos metadados do despacho, e só com `AGENTE_PERMITIR_TELEFONE_DE_TESTE`. Com SIP, o atributo do SIP sempre vence. A T4.6 deve desligar essa opção no ambiente da clínica. | O navegador não tem número de telefone. Os metadados só podem ser escritos com a chave do LiveKit, nunca pelo LLM. |
| T3.2 | Confirmado pelo usuário em 02/11/2026. A recusa de consentimento vale até o fim da chamada: um "sim" depois não a desfaz. Recusar depois de aceitar apaga o aceite, tira o paciente selecionado e desfaz a verificação. | É a leitura mais protetora da H1, e uma recusa nunca vira aceite por erro de áudio. O custo é a pessoa que mudar de ideia precisar da recepção ou de outra ligação. |
| T3.2 | A checagem de consentimento fica em uma dependência das rotas da API e só vale para `origem=voz`. Rota nova de voz: `POST /chamadas/consentimento`, sem `Idempotency-Key`, porque repetir a mesma resposta dá o mesmo resultado. | A regra fica na API, num único lugar, como pede a T3.2. O painel continua sem consentimento de chamada. |
| T3.2 | O cadastro por voz copia `consentimento_em` da chamada, e também preenche o campo de um paciente que já existia sem ele. | Se a mesma pessoa é "cadastrada" de novo com consentimento, o registro passa a ter esse consentimento. |
| T3.2 | O texto do consentimento e a apresentação como "recepcionista virtual" estão em `prompts/recepcionista.md`. O texto é um rascunho, e a revisão é da T6.1. | A H1 exige o pedido. A pessoa precisa saber que fala com uma recepcionista virtual. |
| T3.2 | O desfecho e a transcrição vêm dos eventos da sessão do ADK. Sem consentimento, a transcrição nem sai do agente, e o worker confere de novo no banco antes de gravar. Vale o último desfecho entre agendou, remarcou, cancelou e transferiu; sem nenhum deles, é `duvida` se a pessoa falou, e `abandonou` se não falou. | Duas barreiras para a transcrição. Assim ela também não fica parada na fila nem na DLQ sem consentimento. |
| T3.2 | `apagar_em` já é gravado com `RETENCAO_TRANSCRICAO_DIAS` (padrão: 90), só quando há transcrição guardada. A limpeza continua na T6.1. | Toda transcrição nasce com prazo. |
| T3.2 | Sem transferência disponível, como no navegador, o agente diz que não consegue transferir e oferece pedido de retorno, se houver consentimento. O horário comercial da H7 entra com a T5.6, e a transferência real, com a T4.5. | A Fase 3 não tem SIP, e o `LiveKitToolset` só oferece `transfer_call` em ligação SIP. |
| T3.3 | Usuário e schema `adk_sessoes`, criados por migration, com a senha de `ADK_SESSOES_SENHA`. O usuário só enxerga o próprio schema, e um teste prova que ele não lê `chamadas` nem `pacientes`. O agente passa a declarar `asyncpg`, só como driver do ADK: o teste de isolamento ganhou essa exceção, e o código do agente continua proibido de importar banco. | A T3.3 pede sessões no Postgres, o que exige o driver. O item 2 da Constituição continua valendo para os dados da clínica. |
| T3.3 | A sessão do ADK é apagada ao fim da ligação. Ao subir, o agente também apaga sessões com mais de 1 dia, que sobraram de um processo que morreu. | Os eventos guardam a conversa e os dados do paciente, inclusive de quem recusou o consentimento. A retenção e o descarte só valem de verdade se não houver uma cópia aqui. |
| T3.3 | As tabelas do ADK são criadas quando o agente sobe, antes de aceitar ligações. | Dois jobs criando as tabelas ao mesmo tempo dariam erro. |
| T3.4 | O teste pelo navegador usa uma página própria e mínima (`teste-navegador`, em `http://localhost:8090`, só com `--profile teste`), no lugar do Playground ou do `agent-starter-react`. O `livekit-client` vem do jsDelivr, com versão fixa e hash de integridade. | O navegador não tem número de telefone, e a página escolhe um telefone do seed no despacho. Ela também mostra a transcrição e as ferramentas, e dispensa build de front-end e serviço externo. |
| T3.4 | `ToolsetEmSerie` executa uma ferramenta MCP por vez, na ordem pedida pelo modelo. | O modelo pediu `verificar_identidade` e `listar_meus_agendamentos` no mesmo turno, e o ADK rodou as duas juntas: a API recusou a segunda, e a conversa desandou. Só o prompt não bastou. |
| T3.4 | `end_call` espera 4 s antes de desligar. | O `LiveKitRunner` descarta o áudio que falta ao desligar, o que cortaria a despedida. O tempo é fixo porque o ADK não expõe o fim da reprodução. |
| T3.4 | `scripts/conversa_voz.py` conversa com o agente com voz sintetizada (`say`, do macOS) e mede a latência. Agendar, remarcar e cancelar passaram assim, com o resultado conferido no banco. A página foi testada no Chrome com microfone simulado. | Permite repetir os fluxos sem falar. Não substitui o teste com voz humana, que é o critério da fase. |
| T3.4 | A confirmação do paciente é regra do prompt. No teste, antes de reforçar o prompt, "Essa mesma" chegou a ser aceito como confirmação de um cancelamento. | A API não ouve a conversa. A T3.6 pôs uma trava abaixo do modelo para agendar, remarcar e cancelar. |
| T3.5 | O silêncio de fim de fala fica no padrão do Gemini, ajustável por `AGENTE_SILENCIO_FIM_DE_FALA_MS`. | Encurtar ganha cerca de 0,4 s, mas pode cortar quem faz pausas ao ditar uma data, e cada data errada gasta uma das 3 tentativas. A escolha depende de ouvir, e fica para o teste do usuário. |
| T3.5 | As metas de latência e da busca RAG não foram alteradas, mesmo sem serem atingidas. Os números estão nos riscos. | Mudar uma meta, trocar o modelo de embedding ou fazer o agente dizer "um instante" antes de buscar muda o que o paciente percebe, e a decisão é do usuário. |
| T3.6 | O preparar só lê: a API valida e devolve a fala, sem gravar nada. Quem exige o preparo é a trava do agente, na memória do processo da ligação, e não há tabela nova. As rotas `/remarcar` e `/cancelar` da API não mudaram, para o painel usá-las na T5.2. | A decisão do usuário põe a trava no `before_tool_callback`. A API não ouve a conversa, então um preparo gravado nela não provaria o "sim". |
| T3.6 | Contam como confirmação: "sim", "confirmo", "pode confirmar", "pode cancelar", "pode remarcar" e "pode trocar". Qualquer "não", "pode ser", "talvez", "acho", "espera", "mas", "depende" ou "sei lá", ou uma pergunta, anula a confirmação. | Na dúvida, a trava recusa e o agente pergunta de novo. Errar para o lado da recusa custa uma pergunta, e o erro oposto cancela uma consulta. A ampliação da lista está na decisão seguinte, da pendência de 02/11/2026. |
| T3.6 | A fala precisa ter começado depois do preparo, e os argumentos de executar precisam ser os mesmos do preparo. Uma recusa da trava não gasta o preparo; qualquer outra resposta de executar gasta. | Um "sim" dado antes, como o do consentimento, não confirma nada, nem vale para outra consulta ou outro horário. |
| T3.6 | A trava observa os eventos ao vivo com um `Runner` próprio, e espera até 2 s pela transcrição. Depois de ver a confirmação, espera 0,3 s por mais trechos. | No Gemini 3.x, a chamada de ferramenta chega antes da transcrição final, e os trechos parciais não entram na sessão. Ler `session.events` julgaria a fala anterior. Se a transcrição demorar mais de 2 s, o paciente só ouve a pergunta de novo. |
| T3.6 | Pedido do usuário em 02/11/2026: a trava passa a cobrir `confirmar_agendamento`, e "sim, não tem problema", "não tem problema" e "sem problema" contam como confirmação. Um "não" ou um "problema" que sobre desfaz ("não, não tem problema", "tem problema, sim"). Entram também "pode marcar" e "pode agendar", a resposta natural a "posso confirmar?". | O item 4 da Constituição também vale para agendar. Na dúvida, a trava continua recusando. |
| T3.6 | Agendar não ganhou etapa de preparar: o preparo é o `segurar_horario` que deu certo, identificado pelo `hold_id` da resposta. `cadastrar_paciente` e `selecionar_paciente` renovam o preparo. Se o `hold_id` for outro, a resposta é `preparo_ausente`, e o agente segura de novo: um retry de segurar devolve o mesmo hold. | O contrato MCP não muda. A renovação existe porque o prompt pede um "sim" ao nome e à data do paciente novo, e esse "sim" não pode confirmar a consulta. |
| T3.8 | O CI também confere `ruff format`. As actions ficam fixadas por commit, e o `uv`, na versão dos Dockerfiles. A permissão do token é só de leitura. | É a verificação que o pre-commit já faz. A fixação impede que uma action mude sem aviso, numa pipeline que recebe a chave do Gemini. |
| T3.8 | Sem o segredo `GOOGLE_API_KEY`, a avaliação do RAG é pulada, e o CI continua verde. Pull requests vindos de forks nunca recebem o segredo. Os testes de `test_compose` que exigem a stack no ar também são pulados no CI. | O CI não precisa da chave para o resto. Cada execução com o segredo gasta créditos de embedding: a base e as 30 perguntas. |
| Ajuste | Os testes só falam com o Gemini com `RODAR_TESTES_GEMINI=1`: sem isso, o `conftest.py` esvazia `GOOGLE_API_KEY`, antes do `.env` ser lido. No CI, a chave só é entregue com a variável de repositório `RODAR_TESTES_GEMINI=1`. | A chave do `.env` e o segredo do CI faziam cada `pytest` e cada push gastar crédito sem ninguém pedir. Corrige a decisão da T3.8. |
| T3.7 | O agente toca a frase de um WAV, numa faixa de áudio própria, ao iniciar `buscar_conhecimento`, `listar_horarios_livres` e `listar_meus_agendamentos`, escolhidas pelo usuário em 02/11/2026. Não repete em menos de 10 s, não toca se o modelo falou nos últimos 2 s, e para quando a resposta começa. Sem o arquivo, a ligação segue sem a frase. | O modelo não falava antes da ferramenta. Tocar em paralelo não atrasa a ferramenta, e uma falha no áudio nunca derruba a ligação. |
| T3.7 | O áudio vem de `scripts/gerar_frase_espera.py`, e o WAV não fica no repositório. `--motor gemini` gasta crédito e ainda não foi rodado; `--motor say` usa a voz do macOS, só para testar o caminho do áudio. O modelo padrão `gemini-3.8-flash-tts` vem da documentação de 02/11/2026. | Gerar pelo Gemini gasta crédito, então a primeira geração fica para a validação. |
| T3.7 | A fala do paciente só corta a frase depois de fechar a transcrição da pergunta que causou a ferramenta. Antes disso, a frase toca até o fim, por cima dele. | No Gemini 3.x, a transcrição da pergunta chega depois da chamada de ferramenta e cortaria toda frase logo no início. A frase é curta, e é melhor tocá-la inteira do que soar cortada. |
| T5.1 | A API exige `Authorization: Bearer` em tudo, menos em `/saude`. Há dois tokens: `API_TOKEN_PAINEL`, que age pela clínica sem `X-Chamada-Id`, e `API_TOKEN_SERVICOS` (agente e `mcp-clinica`), que só age dentro de uma chamada e abre chamadas. O painel envia `X-Usuario-Id`, e a API confere no banco a cada pedido se o usuário está ativo e qual é o perfil. | Um serviço interno comprometido não age como a clínica. Desativar um usuário ou mudar o perfil vale na hora, mesmo com a sessão aberta. |
| T5.1 | O painel não toca no banco: login, regras, perfis e auditoria ficam na API. A sessão é um cookie assinado, `HttpOnly`, `SameSite=Strict`, `Secure` por padrão, com 8 h de duração. Escritas vindas de outra origem são recusadas. | Segue o item 2 da Constituição também no painel. `SameSite` e a checagem de origem cobrem o CSRF, sem token em cada formulário. |
| T5.1 | Depois de 5 senhas erradas, o usuário fica bloqueado por 15 min. Senhas têm pelo menos 10 caracteres. Migration nova em `usuarios`: `ativo`, `falhas_de_login` e `bloqueado_ate`. Um usuário nunca é apagado, só desativado, porque a auditoria aponta para ele. A clínica sempre fica com um administrador ativo. | A spec pede login com argon2, mas não define os limites. Foi escolhida a opção mais restritiva que não trava a operação. |
| T5.1 | A auditoria grava na mesma transação da ação. Registra login, tentativas erradas, troca de senha e cada escrita do painel, sem senha nem hash. Nova tela "Auditoria", só para administradores. As ações por voz não entram: `agendamentos.chamada_id` já diz quem agiu. | A Constituição pede auditoria de quem viu e mudou o quê, e um registro que só existe no banco não ajuda a clínica. |
| T5.1 | O primeiro administrador é criado por `python -m clinica_api.criar_admin`, que pede a senha sem mostrar. O seed cria usuários fictícios só para desenvolvimento. | Nenhuma senha padrão vai para a clínica. |
| T5.1 | O painel é publicado só em `127.0.0.1:8080` por padrão. `PAINEL_BIND` abre para a rede, e `PAINEL_COOKIE_SEGURO=false` permite HTTP, opções documentadas no runbook. O HTMX fica no repositório, em `static/`, sem CDN. | A rede local sem HTTPS exporia a senha. O painel precisa funcionar sem internet, mesmo quando o Gemini está fora. |
| T5.2 | A tela da agenda é uma parte HTML que o HTMX busca de novo a cada aviso do SSE, ao reconectar e a cada 30 s. O SSE leva só o tipo do evento, e só para páginas da mesma clínica. Sem `hx-boost`: a navegação é comum, para não sobrar conexão de SSE entre páginas. | A decisão da T1.6 pede para reler do banco, sem depender da ordem nem da entrega dos eventos. Os 30 s cobrem um evento perdido ou o broker fora do ar. |
| T5.2 | Agendar pelo painel segura e confirma no mesmo envio. A recepção escolhe o horário entre os livres e o paciente por busca. Reenviar o formulário responde "ocupado" e não duplica. A tela de pacientes entrou aqui, porque o agendamento manual precisa dela. | O painel confirma na hora, então não precisa de pausa entre segurar e confirmar. A regra de retry é a da spec: um hold que virou consulta não é devolvido. |
| T5.2 | A recepção pode marcar uma consulta confirmada como `faltou` ou `concluido`, o que gera o evento novo `agendamento.atualizado`. Paciente não é apagado pelo painel. A auditoria guarda só o id do paciente, nunca nome, telefone ou nascimento. Ver a lista de pacientes grava um registro, sem o termo buscado. Olhar a agenda não grava. | A H8 mostra o status `faltou`, e alguém precisa marcá-lo. Copiar dados pessoais na auditoria burlaria a retenção. Registrar cada atualização automática da agenda encheria a auditoria. |
| T4.6 | `clinica_core.telefone.normalizar_telefone` veio antes da T4.6, porque o cadastro de pacientes já precisa dela. Aceita o número com ou sem +55, com 0 antes do DDD ou como URI SIP. Sem DDD, ou com "anonymous", devolve vazio: nunca adivinha o DDD. | Um telefone errado identificaria a pessoa errada. É mais seguro tratar como número oculto. |
| T5.3 | Os cadastros são uma tela só, descrita como dados (`clinica_painel/formularios.py`). A API confere que dentista e consultório são da mesma clínica, porque a FK não sabe disso. Um registro em uso não é apagado, e a mensagem sugere desativar. A lista de bloqueios mostra só os que ainda valem. Campos de data e hora valem no fuso da clínica. | Reduz o código de sete telas quase iguais, sem mudar as regras. A lista de bloqueios cresceria sem limite com um almoço por dia. |
| T5.3 | As exclusion constraints da grade usam `tsrange` com uma data fixa, porque `time` não tem tipo de intervalo próprio. Em um banco que já tenha grades sobrepostas, a migration falha ao subir a API e aponta o conflito. | Falhar alto é melhor que apagar um horário em silêncio. O seed não tem sobreposição. |
