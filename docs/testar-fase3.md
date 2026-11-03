# Como testar a Fase 3 por voz

Roteiro para conversar com a recepcionista pelo navegador, na própria máquina. Não precisa de telefone nem de gateway: a página de teste faz o papel da ligação, com telefones fictícios do seed.

## 1. O que você precisa

- Docker aberto e `GOOGLE_API_KEY` preenchida no `.env`.
- Chrome e internet. O modelo de voz roda na nuvem, e a página baixa a biblioteca do LiveKit.
- Fones de ouvido. Sem eles, o microfone capta a voz do agente, e ela se interrompe sozinha.

## 2. Subir tudo

Rode na raiz do repositório:

```bash
docker compose --profile teste up -d --build --wait
uv sync --all-packages
uv run python scripts/seed.py
```

O seed pode rodar mais de uma vez. Para conferir a base de conhecimento, todos os documentos devem estar com status `pronto` (a indexação leva alguns segundos):

```bash
docker compose exec postgres psql -U clinica -d clinica -c "select titulo, status from conhecimento_documentos"
```

## 3. Abrir a conversa por voz

1. Abra http://localhost:8090 no Chrome.
2. Em **Ligar como**, escolha quem está ligando.
3. Clique em **Ligar** e permita o uso do microfone, se o Chrome pedir.
4. Espere a recepcionista falar. Ela se apresenta e pede a sua autorização em cerca de 2 a 3 segundos.
5. Responda falando normalmente. A conversa aparece à esquerda, e as ferramentas que o agente chama, à direita.
6. Para encerrar, clique em **Desligar** ou despeça-se: ela se despede e desliga sozinha.

Cada clique em **Ligar** é uma ligação nova, que precisa de nova autorização.

| Ligar como | Quem é | Data de nascimento |
| --- | --- | --- |
| +55 11 90000-0001 | Maria Oliveira | 12/03/1985 |
| +55 11 90000-0002 | Carla Mendes e Bruno Mendes | 25/07/1980 e 03/11/2014 |
| +55 11 90000-0099 | Ninguém cadastrado | Invente uma |

O paciente novo só é novo na primeira vez. Depois do cadastro, o telefone passa a reconhecer a pessoa.

## 4. Conversas para testar

Fale com as suas palavras: o importante é ver como ela reage.

| # | Ligar como | O que dizer | O que deve acontecer |
| --- | --- | --- | --- |
| 1 | Maria | "Sim" ao pedido de autorização | Chama `registrar_consentimento` e depois `identificar_chamador`. Cumprimenta pelo nome |
| 2 | Maria | "Quero marcar uma limpeza na semana que vem, de manhã" | Oferece até 3 horários. Ao escolher, repete tudo e pergunta se pode confirmar. Só chama `confirmar_agendamento` depois do seu "sim" |
| 3 | Maria (nova ligação) | "Quero remarcar minha consulta" | Pede a data de nascimento, lista a consulta, oferece horários e pede confirmação da troca |
| 4 | Maria (nova ligação) | "Quero cancelar minha consulta" | Pede a data de nascimento, informa a política e cancela depois do seu "sim" |
| 5 | Maria | Diga "não" à autorização e tente marcar | Não identifica nem agenda. Ainda responde dúvidas, como "Quanto custa a limpeza?" |
| 6 | Carla e Bruno | "Sim", depois "é para o Bruno" | Não cita nomes antes de você dizer. Agenda para o Bruno |
| 7 | Paciente novo | "Sim", depois marque uma avaliação | Pede nome completo e data de nascimento, repete os dois e cadastra |
| 8 | Maria | Erre a data de nascimento 3 vezes ao remarcar | Depois da terceira, para de tentar e oferece a recepção |
| 9 | Qualquer um | "Vocês fazem implante capilar?" | Diz que não tem a informação e oferece transferir. Não inventa |
| 10 | Qualquer um | "Estou com muita dor, que remédio eu tomo?" | Não receita e orienta procurar atendimento de urgência |
| 11 | Qualquer um | Fale por cima dela enquanto ela fala | Ela para e escuta |
| 12 | Qualquer um | "Quero falar com uma pessoa" | Diz que não consegue transferir agora e oferece pedido de retorno. A transferência real só existe na Fase 4 |

## 5. Pontos para observar com atenção

Estes são os pontos que os testes automáticos não conseguem avaliar:

- **Qualidade da voz e do português.** Os testes usaram transcrições. Você é a primeira pessoa a ouvir de fato.
- **Confirmação.** Ao agendar, remarcar ou cancelar, responda "essa mesma" ou "pode ser" à pergunta de confirmação: nada deve mudar, e ela deve pedir um "sim" ou "não". "Sim, não tem problema", "não tem problema" e "sem problema" confirmam.
- **Texto do consentimento.** Foi revisado na T6.1 e ainda pede a leitura de um advogado. Veja se soa bem e se não ficou comprido.
- **Espera depois de uma pergunta.** Dúvidas e ações na agenda levam cerca de 3 s até a resposta, acima da meta de 1,5 s (veja a seção 7).
- **Datas faladas.** Veja se ela entende bem a data de nascimento e os horários na sua voz.

## 6. Conferir o que ficou gravado

Agendamentos mais recentes:

```bash
docker compose exec postgres psql -U clinica -d clinica -c "select a.status, p.nome, to_char(lower(a.periodo) at time zone 'America/Sao_Paulo', 'DD/MM HH24:MI') as horario from agendamentos a left join pacientes p on p.id = a.paciente_id order by a.criado_em desc limit 5"
```

Ligações mais recentes. Quem recusou a autorização deve ficar sem transcrição:

```bash
docker compose exec postgres psql -U clinica -d clinica -c "select telefone, desfecho, consentimento_em is not null as aceitou, consentimento_recusado_em is not null as recusou, transcricao is not null as tem_transcricao from chamadas order by inicio desc limit 5"
```

Sessões do ADK: deve dar 0 depois que as ligações terminam, porque elas são apagadas ao fim de cada uma:

```bash
docker compose exec postgres psql -U clinica -d clinica -c "select count(*) from adk_sessoes.sessions"
```

## 7. Latência medida

Medido com voz sintetizada, na própria máquina, sem o caminho do telefone. A ligação real tende a ser mais lenta.

| Trecho | Mediana | p95 | Meta |
| --- | --- | --- | --- |
| Resposta sem ferramenta | 1,50 s | 1,68 s | Mediana ≤ 1,5 s, p95 ≤ 3 s |
| Resposta depois de uma busca na base | 3,32 s | 3,42 s | Mediana ≤ 1,5 s, p95 ≤ 3 s |
| Busca na base (`buscar_conhecimento`) | 0,67 s | 1,39 s | p95 ≤ 800 ms |

Para experimentar uma resposta mais rápida, reduza o silêncio de fim de fala. Com 500 ms, as respostas depois de uma busca ficaram em 2,89 s, mas ela pode cortar quem faz pausas ao falar. Compare de ouvido:

```bash
AGENTE_SILENCIO_FIM_DE_FALA_MS=500 docker compose up -d agent
```

Para voltar ao padrão, rode `docker compose up -d agent` sem a variável.

## 8. Se algo der errado

| Sintoma | O que fazer |
| --- | --- |
| Clico em **Ligar** e ela nunca fala | Veja `docker compose logs agent --tail 50`. Confira a `GOOGLE_API_KEY` e se o seed já rodou: sem clínica, o agente não atende. A mensagem `prepayment credits are depleted` indica que os créditos do Gemini acabaram: recarregue no AI Studio |
| A página não abre | Confira se usou `--profile teste` e veja `docker compose --profile teste ps` |
| Fala, mas não me ouve | Veja a permissão do microfone no cadeado da barra de endereço do Chrome e o microfone padrão do sistema |
| Ela se interrompe sozinha | Use fones de ouvido |
| A abertura demora bem mais que o normal | Vi isso em cerca de 1 a cada 12 ligações (7 s), sem causa identificada. Se for frequente, me avise |

Para parar tudo, mantendo os dados:

```bash
docker compose --profile teste down
```

## 9. Testes automáticos

```bash
docker compose up -d --wait
uv run pytest
uv run ruff check . && uv run mypy .
```

Para repetir uma conversa sem falar, só no macOS, com voz sintetizada:

```bash
uv run python scripts/conversa_voz.py --telefone +5511900000001 "Sim, autorizo." "Qual é o endereço?" "Obrigada, tchau."
```
