#!/usr/bin/env bash
# entrypoint.sh — cron запускает задания с "чистым" окружением и не видит
# переменные, заданные через ENV в Dockerfile / environment в
# docker-compose.yml. Debian-сборка cron перед выполнением заданий читает
# /etc/environment — прописываем туда нужные переменные один раз при
# старте контейнера, затем запускаем cron на переднем плане (PID 1),
# чтобы контейнер не завершался.
set -euo pipefail

{
  echo "VAULT_DIR=${VAULT_DIR:-/vault/55-sleepmon}"
  echo "BACKUP_DIR=${BACKUP_DIR:-/backups}"
  echo "BACKUP_DB=${BACKUP_DB:-/backups/sleepmon.sqlite}"
  echo "LOG_FILE=${LOG_FILE:-/backups/backup.log}"
} > /etc/environment

mkdir -p "${BACKUP_DIR:-/backups}"
touch "${LOG_FILE:-/backups/backup.log}"

echo "$(date -Iseconds) контейнер dbtool запущен; ежедневный бэкап в 01:00" >> "${LOG_FILE:-/backups/backup.log}"

exec cron -f
