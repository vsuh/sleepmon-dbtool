#!/usr/bin/env bash
# backup.sh — обёртка над sleepmon_dbtool.py для запуска из cron внутри
# контейнера dbtool. Пути захардкожены через ENV в Dockerfile (см. рядом),
# но могут быть переопределены переменными окружения при отладке.
set -uo pipefail
# Намеренно без -e: нужно поймать код завершения python3 и всё равно
# дописать финальную строку в лог, а не оборвать скрипт на середине.

VAULT_DIR="${VAULT_DIR:-/vault/55-sleepmon}"
BACKUP_DB="${BACKUP_DB:-/backups/sleepmon.sqlite}"
LOG_FILE="${LOG_FILE:-/backups/backup.log}"

mkdir -p "$(dirname "$BACKUP_DB")"

{
  echo "=== $(date -Iseconds) backup start (root=$VAULT_DIR, db=$BACKUP_DB) ==="
  python3 /opt/dbtool/sleepmon_dbtool.py backup \
      --root "$VAULT_DIR" \
      --db "$BACKUP_DB" \
      --prune
  status=$?
  echo "=== $(date -Iseconds) backup finished (exit $status) ==="
} >> "$LOG_FILE" 2>&1

exit "$status"
