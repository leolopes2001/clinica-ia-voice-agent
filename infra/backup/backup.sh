#!/bin/sh
# Backup diário do Postgres e dos arquivos da base de conhecimento.
# `backup.sh --uma-vez` faz um backup e sai. Sem argumentos, fica no ar e roda todo dia.
set -u

LOCAL=/backups
EXTERNO=/externo
ESTADO="$LOCAL/ultimo.json"
HORA="${BACKUP_HORA:-03:30}"
RETENCAO="${BACKUP_RETENCAO_DIAS:-14}"
ESPERA_APOS_FALHA_S=3600
UM_DIA_MIN=1440

# Os dumps têm dados de saúde: só o dono lê.
umask 077

log() { printf '%s %s\n' "$(date -Iseconds)" "$*"; }

gravar_estado() {
  # $1 situação da cópia local; $2 situação da cópia externa; $3 arquivo; $4 tamanho
  temporario="$ESTADO.tmp"
  printf '{"quando":"%s","situacao":"%s","externo":"%s","arquivo":"%s","tamanho":%s}\n' \
    "$(date -Iseconds)" "$1" "$2" "$3" "$4" >"$temporario"
  # O estado não tem dado sensível, e o painel lê para a tela de saúde.
  chmod 644 "$temporario"
  mv "$temporario" "$ESTADO"
}

copiar_para_fora() {
  # $1 arquivo. Só sai da máquina criptografado.
  destino="$EXTERNO/$(basename "$1").enc"
  openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_SENHA -in "$1" -out "$destino.tmp" &&
    mv "$destino.tmp" "$destino"
}

fazer_backup() {
  carimbo="$(date +%Y%m%d-%H%M%S)"
  dump="$LOCAL/clinica-$carimbo.dump"
  arquivos="$LOCAL/conhecimento-$carimbo.tar.gz"

  if ! pg_dump -Fc -f "$dump.tmp"; then
    log "ERRO: pg_dump falhou"
    rm -f "$dump.tmp"
    gravar_estado falhou nao_tentado "" 0
    return 1
  fi
  # Um dump que não abre é pior que nenhum: ninguém descobre até precisar dele.
  if ! pg_restore --list "$dump.tmp" >/dev/null; then
    log "ERRO: o dump gerado não abre"
    rm -f "$dump.tmp"
    gravar_estado falhou nao_tentado "" 0
    return 1
  fi
  mv "$dump.tmp" "$dump"

  if ! tar -czf "$arquivos.tmp" -C /conhecimento .; then
    log "ERRO: falha ao copiar os arquivos da base de conhecimento"
    rm -f "$arquivos.tmp"
    gravar_estado falhou nao_tentado "$(basename "$dump")" 0
    return 1
  fi
  mv "$arquivos.tmp" "$arquivos"

  externo=sem_senha
  if [ -n "${BACKUP_SENHA:-}" ]; then
    if copiar_para_fora "$dump" && copiar_para_fora "$arquivos"; then
      externo=ok
    else
      externo=falhou
      log "ERRO: falha na cópia externa"
      rm -f "${EXTERNO:?}"/*.tmp
    fi
  else
    log "AVISO: sem BACKUP_SENHA, não há cópia externa"
  fi

  find "$LOCAL" -maxdepth 1 -type f \( -name 'clinica-*.dump' -o -name 'conhecimento-*.tar.gz' \) \
    -mtime +"$RETENCAO" -delete
  find "$EXTERNO" -maxdepth 1 -type f -name '*.enc' -mtime +"$RETENCAO" -delete

  tamanho="$(wc -c <"$dump" | tr -d ' ')"
  gravar_estado ok "$externo" "$(basename "$dump")" "$tamanho"
  log "Backup concluído: $(basename "$dump"), $tamanho bytes, cópia externa: $externo"
  [ "$externo" != falhou ]
}

segundos_ate_o_horario() {
  agora="$(date +%s)"
  alvo="$(date -d "today $HORA" +%s)"
  if [ "$alvo" -le "$agora" ]; then
    alvo="$(date -d "tomorrow $HORA" +%s)"
  fi
  echo $((alvo - agora))
}

if [ "${1:-}" = "--uma-vez" ]; then
  fazer_backup
  exit $?
fi

# Ao subir: uma máquina que passa a noite desligada não fica sem backup.
if ! find "$LOCAL" -maxdepth 1 -name 'clinica-*.dump' -mmin -"$UM_DIA_MIN" | grep -q .; then
  fazer_backup || sleep "$ESPERA_APOS_FALHA_S"
elif [ ! -f "$ESTADO" ]; then
  gravar_estado ok nao_tentado "" 0
fi

while :; do
  espera="$(segundos_ate_o_horario)"
  log "Próximo backup em $espera s"
  sleep "$espera"
  until fazer_backup; do
    sleep "$ESPERA_APOS_FALHA_S"
  done
done
