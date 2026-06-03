#!/bin/bash
echo "[$(date)] Running startup tasks..."

# Провижин схем/таблиц + per-tenant ролей и грантов (#18) ОДИН раз. Запускается
# ПОД admin-ролью (hh) напрямую, НЕ через run_all (tenant-роли не имеют CREATE).
# Идемпотентно: схемы/роли/пароли создаются один раз и переиспользуются.
/usr/local/bin/python /app/provision_roles.py

# Мультиюзер: run_all проходит по всем активным юзерам из public.app_users.
# Настройки apply-similar (resume_id, use_ai) берутся из БД (PG settings).
/usr/local/bin/python /app/run_all.py -- /usr/local/bin/python -m hh_applicant_tool refresh-token
/usr/local/bin/python /app/run_all.py -- /usr/local/bin/python -m hh_applicant_tool update-resumes
# НЕ запускаем apply-similar при старте — иначе up -d мгновенно разошлёт пачку.
# Рассылка только по cron (0 8-21).

echo "[$(date)] Startup tasks finished."
