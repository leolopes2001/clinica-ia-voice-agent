# Runbook da recepcionista de voz

O que fazer quando algo para de funcionar. Comece sempre pela tela **Saúde** do painel e por:

```bash
docker compose ps
docker compose logs <serviço> --since 15m
```

Os logs são JSON, uma linha por evento, e não trazem falas nem dados de paciente.

## O que o paciente percebe em cada falha

| O que caiu | O que quem liga percebe | O que continua funcionando |
| --- | --- | --- |
| Internet | A ligação é atendida e cai sem nenhuma fala | Painel, agenda manual, backup |
| API do Gemini ou créditos | A ligação é atendida e cai sem nenhuma fala | Painel, agenda manual, backup |
| Gateway | Sinal de ocupado, ou a operadora diz que está indisponível | Painel, agenda manual |
| Agente ou LiveKit | A ligação não completa, ou cai em seguida | Painel, agenda manual |
| API ou Postgres | O agente diz que o sistema está com dificuldade e oferece transferir | Quase nada: a agenda fica fora do ar |
| RabbitMQ | Nada, na hora. A agenda ainda atualiza em até 30 s, e os holds vencem pela varredura | Tudo, com atraso |

**A defesa contra as duas primeiras é o gateway.** Configure nele o desvio para a recepção quando a chamada não for atendida ou cair (veja "Plano B no gateway"). O sistema não toca uma mensagem gravada por conta própria: sem o agente, não há quem toque.

## Caiu a internet

**Como notar:** as ligações caem sem fala. O agente registra erro de conexão ao Gemini. O painel continua abrindo.

1. Confirme: `ping -c 3 generativelanguage.googleapis.com`.
2. Ligue o desvio do gateway para a recepção, se ele não for automático.
3. Avise a recepção para agendar pelo painel, que funciona sem internet.
4. Quando a internet voltar, não é preciso reiniciar nada: a próxima ligação abre uma conexão nova. Faça uma ligação de teste.

## Caiu a API do Gemini, ou acabaram os créditos

**Como notar:** o log do agente traz o código `1011` ou o texto `prepayment credits are depleted` (créditos), ou erros `5xx`, `429` ou `403` (serviço, limite ou chave).

```bash
docker compose logs agent --since 1h | grep -Ei "1011|credits|429|403|unavailable"
```

| Causa | O que fazer |
| --- | --- |
| Créditos esgotados | Recarregue no AI Studio. Vale configurar um alerta de saldo lá. A próxima ligação já funciona |
| Limite de uso (`429`) | Espere. Se for frequente, reveja o plano no AI Studio |
| Chave recusada (`403`) | Confira `GOOGLE_API_KEY` no `.env` e rode `docker compose up -d agent worker mcp-conhecimento` |
| Modelo não existe mais (`404`) | Os modelos de voz são de pré-lançamento e mudam. Troque `AGENTE_MODELO_VOZ` no `.env` e rode `docker compose up -d agent` |
| Fora do ar no Google | Consulte a página de status do Google AI. Ligue o desvio do gateway enquanto durar |

Enquanto isso, ligue o desvio do gateway. **Sem embeddings**, a busca funciona só por texto, um pouco pior, e a indexação de documentos novos vai para `erro`: use **Indexar de novo** quando voltar.

## Caiu o gateway ou o chip

**Como notar:** o celular de teste recebe sinal de ocupado ou "indisponível", e não aparece chamada nova na tela **Chamadas**. `docker compose logs livekit-sip --since 15m` não mostra `INVITE`.

1. Confira a energia, o cabo de rede e a antena do gateway, e veja o sinal e o chip na interface dele.
2. Teste ligando do chip para outro número.
3. Faça `ping` no IP do gateway a partir do servidor.
4. Confira se o IP dele ainda é o que está em `SIP_GATEWAY_IP` no `.env`. Um IP novo exige rodar `scripts/bootstrap_sip.sh` de novo.
5. Se o `livekit-sip` recebe o `INVITE` mas recusa, o trunk ou a dispatch rule se perdeu junto com o Redis. Rode `scripts/bootstrap_sip.sh`, que pode ser repetido.

## Plano B no gateway

Na interface do gateway, configure, se o modelo permitir:

- **Desvio quando não atender ou ao falhar o destino SIP** para o celular ou o ramal da recepção.
- Um tempo curto antes do desvio, como 2 toques.

Teste com o agente parado (`docker compose stop agent`) e uma ligação real. Isso depende do modelo do gateway, então registre aqui o que foi feito na sua instalação.

## Caiu a API ou o Postgres

**Como notar:** a tela **Saúde** mostra o alerta, ou o painel diz "O sistema está com dificuldade".

```bash
docker compose ps postgres api
docker compose logs postgres --since 15m
df -h
```

1. Se o disco estiver cheio, libere espaço. Backups antigos em `backups` são os primeiros candidatos, depois de confirmar a cópia externa.
2. `docker compose up -d --wait`.
3. Se o banco estiver corrompido, siga "Restaurar um backup".

A API aplica as migrations ao subir. Uma migration que falha aparece em `docker compose logs api`, e a API não sobe. A que impede grades sobrepostas falha se já houver sobreposição: corrija a grade no banco e suba de novo.

## Mensagens em uma DLQ

Mensagem com falha fica parada em `<fila>.dlq`, e a tela **Saúde** avisa.

| Fila | O que significa | O que fazer |
| --- | --- | --- |
| `conhecimento.indexar.dlq` | Um documento não indexou | Veja o documento com status `erro` e use **Indexar de novo**. A mensagem antiga pode ser descartada |
| `chamada.encerrada.dlq` | O fim de uma chamada não foi gravado. Pode conter a transcrição | Veja o erro em `docker compose logs worker`. Devolva à fila depois de corrigir, ou descarte em até `RETENCAO_TRANSCRICAO_DIAS` dias |
| `holds.expirar.dlq` | Uma mensagem de expiração falhou | Pode descartar: a varredura de 60 s libera o horário de qualquer forma |
| `manutencao.diaria.dlq` | A retenção falhou nesse dia | Veja o erro nos logs. Pode descartar: ela roda de novo em até 24 h |

Para ver e descartar: `http://localhost:15672` (usuário e senha do RabbitMQ), em **Queues**, abra a fila e use **Get messages** ou **Purge**. Essa porta só abre na própria máquina.

## Backup

Roda todo dia às 03:30, e ao subir se não houver um das últimas 24 h. Fica no volume `backups` por `BACKUP_RETENCAO_DIAS` dias, e a cópia externa, criptografada, na pasta de `BACKUP_DESTINO_EXTERNO`.

Para rodar agora:

```bash
docker compose exec backup sh /backup/backup.sh --uma-vez
```

**A senha da cópia externa (`BACKUP_SENHA`) precisa estar guardada fora do servidor.** Sem ela, a cópia não abre. Uma transcrição apagada pela retenção continua nos backups por até `BACKUP_RETENCAO_DIAS` dias.

Faça um teste de restauração pelo menos a cada 3 meses.

### Restaurar um backup

**Conferir um backup, sem tocar em nada**, restaurando num banco à parte:

```bash
docker compose exec backup ls -1 /backups
scripts/restaurar_backup.sh clinica-AAAAMMDD-HHMMSS.dump clinica_restaurado
docker compose exec backup dropdb clinica_restaurado
```

**Trocar o banco em uso** por um backup. Isso apaga o que foi gravado depois dele:

```bash
docker compose stop agent api painel worker mcp-clinica mcp-conhecimento
docker compose exec backup dropdb --force clinica
docker compose exec backup createdb clinica
docker compose exec backup pg_restore --exit-on-error -d clinica /backups/clinica-AAAAMMDD-HHMMSS.dump
docker compose up -d --wait
```

**Num servidor novo, a partir da cópia externa:**

```bash
# 1. Decifrar, na máquina onde está a cópia.
BACKUP_SENHA='a senha' openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_SENHA \
  -in clinica-AAAAMMDD-HHMMSS.dump.enc -out clinica.dump

# 2. Subir só o banco, com os mesmos POSTGRES_* do .env antigo, e criar o usuário das sessões.
docker compose up -d --wait postgres
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "CREATE ROLE adk_sessoes LOGIN PASSWORD 'o valor de ADK_SESSOES_SENHA'"

# 3. Restaurar e subir o resto.
docker compose cp clinica.dump postgres:/tmp/clinica.dump
docker compose exec postgres pg_restore --exit-on-error -U "$POSTGRES_USER" -d "$POSTGRES_DB" /tmp/clinica.dump
docker compose exec postgres rm /tmp/clinica.dump
rm clinica.dump
docker compose up -d --wait
```

Este caminho foi testado num Postgres novo em 02/11/2026. Os arquivos da base de conhecimento ficam em `conhecimento-AAAAMMDD-HHMMSS.tar.gz`, com a mesma criptografia: decifre e extraia no volume do worker com `docker compose cp`.

## Trocar as senhas

Faça isso antes de atender o primeiro paciente, com a stack ainda sem dados importantes.

```bash
uv run python scripts/gerar_segredos.py
```

O script grava no `.env` os segredos que faltam, sem mostrar os valores, e nunca troca um que já exista. Os serviços leem os novos valores ao recriar os contêineres, mas **duas senhas já estão gravadas dentro do banco**, e precisam mudar lá primeiro. Com o Compose lendo o novo `.env`:

```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

```sql
ALTER ROLE clinica PASSWORD 'o novo POSTGRES_PASSWORD';
ALTER ROLE adk_sessoes PASSWORD 'o novo ADK_SESSOES_SENHA';
```

A senha do RabbitMQ também fica gravada no volume dele:

```bash
docker compose exec rabbitmq rabbitmqctl change_password clinica 'o novo RABBITMQ_PASSWORD'
```

Depois, recrie tudo e confira na tela **Saúde** que o alerta de senhas sumiu:

```bash
docker compose up -d --force-recreate --wait
```

Troque também `LIVEKIT_API_KEY`, se quiser, e deixe `AGENTE_PERMITIR_TELEFONE_DE_TESTE` desligado. Trocar `PAINEL_SEGREDO_SESSAO` desconecta quem estiver no painel.

## Acesso ao painel pela rede

Por padrão, o painel só abre na própria máquina. Para a recepção acessar de outro computador, é preciso HTTPS: sem ele, a senha e os dados de paciente trafegam abertos na rede.

1. Ponha um proxy reverso com HTTPS na frente (Caddy ou Nginx), publicando na rede e encaminhando para `127.0.0.1:8080`. É o caminho recomendado, e `PAINEL_BIND` fica como está.
2. Se decidir usar HTTP na rede, por sua conta e risco: `PAINEL_BIND=0.0.0.0` e `PAINEL_COOKIE_SEGURO=false` no `.env`. O navegador não devolve um cookie seguro por HTTP fora de `localhost`, e o login pareceria não funcionar.

## Ligação sem som ou agente mudo

| Sintoma | Causa provável | O que fazer |
| --- | --- | --- |
| O agente atende e não fala nada | Gemini, créditos ou chave | Veja "Caiu a API do Gemini" |
| O agente não ouve | RTP bloqueado entre o gateway e o `livekit-sip` | Confira a faixa UDP 10000-20000 e `LIVEKIT_NODE_IP` |
| Voz picotada | Rede saturada, ou codec | Use G.711 no gateway |
| Sem frase de espera | Falta `infra/audio/frase_espera.wav` | Gere com `scripts/gerar_frase_espera.py`. Sem o arquivo, a ligação segue sem a frase |
| A transferência falha | Destino vazio, ou o gateway não aceita SIP REFER | Veja **Configurações** e o log do agente |
