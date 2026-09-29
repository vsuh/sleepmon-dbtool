#!/usr/bin/env bash
# restore.sh — обёртка над sleepmon_dbtool.py restore для запуска вручную,
# например: docker compose exec dbtool restore.sh --dry-run
set -uo pipefail

VAULT_DIR="${VAULT_DIR:-/vault/55-sleepmon}"
BACKUP_DB="${BACKUP_DB:-/backups/sleepmon.sqlite}"

usage() {
  cat <<EOF
Использование: restore.sh [опции]

  --out DIR            куда восстанавливать (по умолчанию: \$VAULT_DIR = $VAULT_DIR)
  --db PATH            путь к БД (по умолчанию: \$BACKUP_DB = $BACKUP_DB)
  --date-from DATE      YYYY-MM-DD
  --date-to DATE        YYYY-MM-DD
  --path-glob GLOB      например "2026/08/*.md"
  --include-deleted     восстанавливать и помеченные удалёнными карточки
  --force                перезаписывать файлы, отличающиеся от версии в БД
  --dry-run              только показать, что будет сделано
  -h, --help

Все опции, кроме --out и --db, передаются как есть в sleepmon_dbtool.py.
EOF
}

OUT_DIR="$VAULT_DIR"
DB_PATH="$BACKUP_DB"
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT_DIR="$2"; shift 2 ;;
    --db) DB_PATH="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) EXTRA_ARGS+=("$1"); shift ;;
  esac
done

if [[ ! -f "$DB_PATH" ]]; then
  echo "БД не найдена: $DB_PATH" >&2
  exit 1
fi

if [[ "$OUT_DIR" == "$VAULT_DIR" ]]; then
  echo "Внимание: восстановление идёт прямо в примонтированный vault ($OUT_DIR)." >&2
  echo "Рекомендуется предварительно остановить/закрыть Obsidian и запустить с --dry-run." >&2
fi

echo "Восстановление из $DB_PATH в $OUT_DIR"
python3 /opt/dbtool/sleepmon_dbtool.py restore \
    --db "$DB_PATH" \
    --out "$OUT_DIR" \
    "${EXTRA_ARGS[@]}"
