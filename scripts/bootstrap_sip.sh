#!/bin/sh
# Cria o trunk de entrada e a dispatch rule. Precisa de SIP_GATEWAY_IP no .env e da stack no ar.
# Pode rodar de novo: troca o que já existe. Rode depois de mudar o IP do gateway ou de perder o Redis.
set -eu
cd "$(dirname "$0")/.."
docker compose exec -T agent python -m clinica_agent.bootstrap_sip
