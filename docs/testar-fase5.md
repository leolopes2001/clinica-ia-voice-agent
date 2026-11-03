# Como testar o painel (Fase 5)

Roteiro para quem administra a clínica. Não gasta crédito do Gemini, desde que você use `--sem-conhecimento` no seed e não envie documentos.

## 1. Subir

Se a porta 8080 estiver ocupada na sua máquina, defina outra antes, como `PAINEL_PORTA=8088` no `.env`.

```bash
docker compose up -d --build --wait
uv sync --all-packages
uv run python scripts/seed.py --sem-conhecimento
```

Abra http://localhost:8080 (ou a porta que escolheu) no Chrome. O seed cria dois usuários de exemplo, com a senha `senha-de-exemplo`:

| E-mail | Perfil |
| --- | --- |
| `admin@exemplo.test` | Administrador |
| `recepcao@exemplo.test` | Recepção |

Na clínica, não use o seed. Crie o primeiro administrador assim:

```bash
docker compose exec api python -m clinica_api.criar_admin --email voce@suaclinica.com.br
```

## 2. O que conferir

| # | Faça | O que deve acontecer |
| --- | --- | --- |
| 1 | Erre a senha 5 vezes | A conta fica bloqueada por 15 minutos |
| 2 | Entre como recepção | O menu não mostra os cadastros, as chamadas nem a auditoria |
| 3 | Abra a agenda em duas janelas e marque uma consulta em uma delas | A outra janela mostra a consulta sozinha, em até 2 s |
| 4 | Tente marcar o mesmo horário em outra aba que já estava aberta | "Esse horário acabou de ser ocupado" |
| 5 | Remarque e cancele a consulta | O horário antigo volta a ficar livre, e o motivo aparece na consulta |
| 6 | Marque a falta de uma consulta | O status muda para `faltou` |
| 7 | Como administrador, crie um horário de grade que se sobreponha a outro | O painel recusa e explica |
| 8 | Crie um procedimento sem dentista habilitado | Aparece um aviso no alto da tela |
| 9 | Veja **Auditoria** | Tudo o que você fez aparece, com o seu e-mail e sem dados de paciente |
| 10 | Mude a saudação, a voz e o horário em **Configurações** | Salva. O efeito na ligação só dá para ouvir com crédito |

**Base de conhecimento:** enviar um documento gasta crédito de embeddings. Sem crédito ou sem chave, o documento fica com status `erro`, e "Testar uma pergunta" responde só pela busca por texto.
