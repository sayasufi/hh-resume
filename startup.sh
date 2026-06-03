#!/bin/bash
echo "[$(date)] Running startup tasks..."

# Провижин схемы каждого юзера ОДИН раз (таблицы/индексы/триггеры),
# чтобы горячий путь не гонял DDL на каждый коннект.
/usr/local/bin/python /app/run_all.py -- /usr/local/bin/python -c "import hh_applicant_tool.storage.pgconn as p; p.connect(ensure=True).close()"

# Мультиюзер: run_all проходит по всем активным юзерам из public.app_users.
# Настройки apply-similar (resume_id, use_ai) берутся из БД (PG settings).
/usr/local/bin/python /app/run_all.py -- /usr/local/bin/python -m hh_applicant_tool refresh-token
/usr/local/bin/python /app/run_all.py -- /usr/local/bin/python -m hh_applicant_tool update-resumes
# НЕ запускаем apply-similar при старте — иначе up -d мгновенно разошлёт пачку.
# Рассылка только по cron (0 8-21).

echo "[$(date)] Startup tasks finished."
