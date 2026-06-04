#!/bin/bash
echo "[$(date)] Running startup tasks..."

# Перезагружаем crontab из примонтированного /app/crontab (том .:/app), чтобы
# правки расписания подхватывались без пересборки образа.
crontab -u docker /app/crontab 2>/dev/null || true

# Создаём общие таблицы (схема public, разделение по колонке account) — идемпотентно.
/usr/local/bin/python -c "from hh_applicant_tool.storage import pgconn; pgconn.connect(ensure=True).close()"

# Мультиюзер: run_all проходит по всем активным юзерам из public.app_users.
# Настройки apply-similar (resume_id, use_ai) берутся из БД (PG settings).
/usr/local/bin/python /app/run_all.py -- /usr/local/bin/python -m hh_applicant_tool refresh-token
/usr/local/bin/python /app/run_all.py -- /usr/local/bin/python -m hh_applicant_tool update-resumes
# НЕ запускаем apply-similar при старте — иначе up -d мгновенно разошлёт пачку.
# Рассылка только по cron (0 8-21).

# Слушатель /connect (привязка Telegram кандидата по QR) — стартуем сразу;
# watchdog в cron поднимет, если упадёт.
/bin/bash /app/run_listener.sh

echo "[$(date)] Startup tasks finished."
