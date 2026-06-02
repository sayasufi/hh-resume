#!/bin/bash
echo "[$(date)] Running startup tasks..."

# echo "Current user: $(whoami)"
# echo "$CONFIG_DIR"

# Выполняем цепочку
# Настройки для apply-similar (resume_id и use_ai) берутся из базы данных
/usr/local/bin/python -m hh_applicant_tool refresh-token
/usr/local/bin/python -m hh_applicant_tool update-resumes
# НЕ запускаем при старте контейнера — иначе docker compose up -d мгновенно разошлёт пачку откликов.
# Рассылка идёт только по расписанию cron (0 8-21). Чтобы разослать вручную: docker compose run --rm --user docker hh_applicant_tool python -m hh_applicant_tool apply-similar
# /usr/local/bin/python -m hh_applicant_tool apply-similar

echo "[$(date)] Startup tasks finished."
