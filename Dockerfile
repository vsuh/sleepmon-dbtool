# tools/dbtool/Dockerfile
#
# Контейнер бэкапа/восстановления карточек самочувствия. Раз в сутки по cron
# сохраняет 55-sleepmon/*.md в компактную SQLite-БД; restore.sh запускается
# вручную через `docker compose exec dbtool restore.sh ...`.
#
# Пути внутри контейнера захардкожены здесь намеренно (а не только в
# docker-compose.yml) — контейнер должен работать одинаково, откуда бы его
# ни собирали и ни запускали; наружу настраивается только то, ЧТО
# монтируется в эти пути (см. volumes в docker-compose.yml корневого
# проекта), а не то, ГДЕ внутри контейнера их искать.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        cron tzdata \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=Europe/Moscow

# Каталог с картами и каталог бэкапов внутри контейнера — фиксированные пути.
ENV VAULT_DIR=/vault/55-sleepmon \
    BACKUP_DIR=/backups \
    BACKUP_DB=/backups/sleepmon.sqlite \
    LOG_FILE=/backups/backup.log

WORKDIR /opt/dbtool

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sleepmon_dbtool.py backup.sh restore.sh ./
RUN chmod +x backup.sh restore.sh

# Ежедневный запуск backup.sh в 01:00 по времени контейнера (TZ=Europe/Moscow).
COPY crontab /etc/cron.d/sleepmon-dbtool
RUN chmod 0644 /etc/cron.d/sleepmon-dbtool \
    && crontab /etc/cron.d/sleepmon-dbtool

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && mkdir -p /vault /backups

VOLUME ["/vault", "/backups"]

ENTRYPOINT ["/entrypoint.sh"]
