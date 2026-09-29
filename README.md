# sleepmon-dbtool

Бэкап/восстановление ежедневных карточек самочувствия проекта
[vsuh/sleepmon](https://github.com/vsuh/sleepmon) в компактную SQLite-базу.
Подключается к основному проекту как git-субмодуль в `tools/dbtool` и
разворачивается отдельным сервисом `dbtool` в его `docker-compose.yml`.

## Идея

Схема frontmatter карточки (`55-sleepmon/<год>/<месяц>/<дата>.md`) со временем
меняется — поля добавляются, переименовываются, убираются. Поэтому в БД нет
отдельной колонки на каждое поле: весь YAML-frontmatter каждой карточки
хранится целиком как JSON-строка в одной колонке таблицы `cards`, а тело
заметки — в другой. Изменение состава полей не требует миграции БД и не
ломает ни бэкап, ни восстановление.

Подробности схемы БД, работа `content_hash`, мягкое удаление (`is_deleted`)
и т.д. — в комментариях `sleepmon_dbtool.py`.

## Состав

| Файл | Назначение |
|---|---|
| `sleepmon_dbtool.py` | Основной CLI (`backup` / `restore` / `list` / `verify`), не зависит от Docker |
| `backup.sh` | Обёртка для cron: инкрементальный бэкап с `--prune` |
| `restore.sh` | Обёртка для ручного восстановления |
| `Dockerfile` | Контейнер с cron, python и захардкоженными путями `/vault`, `/backups` |
| `crontab` | Расписание: ежедневно в 01:00 |
| `entrypoint.sh` | Пробрасывает ENV в cron, запускает `cron -f` на переднем плане |

## Пути внутри контейнера (захардкожены в Dockerfile)

- `/vault` — весь Obsidian vault (или хотя бы `55-sleepmon`), монтируется с хоста
- `VAULT_DIR=/vault/55-sleepmon` — каталог с карточками
- `/backups` — каталог для БД и логов, монтируется с хоста отдельным volume
- `BACKUP_DB=/backups/sleepmon.sqlite`
- `LOG_FILE=/backups/backup.log`

Снаружи настраивается только то, **что** мигрируется в эти пути (volumes в
`docker-compose.yml`), а не то, где внутри контейнера их искать — это и
имелось в виду под "захардкодить пути в Dockerfile".

## Использование как субмодуля в vsuh/sleepmon

В `docker-compose.yml` основного проекта:

```yaml
  dbtool:
    build:
      context: ./tools/dbtool
    container_name: sleepmon_dbtool
    environment:
      - TZ=Europe/Moscow
    volumes:
      - /mnt/hdd/syncthing/AI.obsdn:/vault:rw
      - /mnt/hdd/backups/sleepmon:/backups:rw
    restart: unless-stopped
```

Vault монтируется в режиме `rw`, чтобы `restore.sh` мог писать обратно в
карточки при аварийном восстановлении; ежедневный `backup.sh` при этом
только читает файлы.

### Ежедневный бэкап

Выполняется автоматически по cron внутри контейнера в 01:00. Ручной запуск:

```bash
docker compose exec dbtool backup.sh
docker compose exec dbtool cat /backups/backup.log
```

### Восстановление

```bash
# Сначала посмотреть, что будет сделано
docker compose exec dbtool restore.sh --dry-run

# Восстановить всё, что отличается от диска
docker compose exec dbtool restore.sh --force

# Восстановить один диапазон дат
docker compose exec dbtool restore.sh --date-from 2026-08-01 --date-to 2026-08-31

# Вернуть конкретную случайно удалённую карточку
docker compose exec dbtool restore.sh --include-deleted --path-glob "2026/08/2026-08-08.md"
```

Перед восстановлением в реальный vault рекомендуется остановить/закрыть
Obsidian (`docker compose stop obsidian`), а после — снова его запустить,
чтобы он перечитал файлы с диска: инструмент пишет напрямую в файловую
систему, в обход Local REST API (осознанно — бэкап/восстановление должны
работать и тогда, когда сам Obsidian недоступен).

### Локальный запуск без Docker

```bash
pip install -r requirements.txt
python3 sleepmon_dbtool.py backup --root /path/to/55-sleepmon --db ./sleepmon.sqlite --prune
python3 sleepmon_dbtool.py restore --db ./sleepmon.sqlite --out ./restored --dry-run
python3 sleepmon_dbtool.py list --db ./sleepmon.sqlite
python3 sleepmon_dbtool.py verify --root /path/to/55-sleepmon --db ./sleepmon.sqlite
```

Полный список опций каждой команды: `python3 sleepmon_dbtool.py <команда> --help`.
