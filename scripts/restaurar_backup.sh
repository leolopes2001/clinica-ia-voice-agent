#!/bin/sh
# Restaura um backup num banco novo, sem tocar no banco em uso.
#   scripts/restaurar_backup.sh clinica-20261103-033000.dump [banco-novo]
# Para um arquivo .enc da cópia externa, veja docs/runbook.md. Trocar de banco é um passo à parte.
set -eu

ARQUIVO="${1:?Informe o arquivo do backup, como clinica-20261103-033000.dump}"
BANCO="${2:-clinica_restaurado}"

cd "$(dirname "$0")/.."
docker compose exec -T backup sh -eu -c '
  createdb "$0"
  # O dono do schema das sessões do ADK: num servidor novo, ele ainda não existe.
  psql -q -d "$0" -c "DO \$\$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '"'"'adk_sessoes'"'"') THEN
      CREATE ROLE adk_sessoes LOGIN;
    END IF; END \$\$"
  pg_restore --exit-on-error -d "$0" "/backups/$1"
' "$BANCO" "$ARQUIVO"
docker compose exec -T backup psql -d "$BANCO" -Atc \
  "select 'clinicas: ' || count(*) from clinicas; select 'agendamentos: ' || count(*) from agendamentos"
echo "Restaurado em $BANCO. O banco em uso não mudou."
