#!/bin/bash
# Запуск Mini App бэкенда (uvicorn) с detach через setsid, чтобы переживал
# завершение родителя (cron-reap). Слушает только 127.0.0.1 — наружу через nginx+TLS.
# Имя файла отдельное, чтобы строка "uvicorn web_app" не попадала в команду watchdog'а.
cd /app && setsid /usr/local/bin/python -u -m uvicorn web_app:app \
  --host 127.0.0.1 --port 60080 \
  >> /var/log/cron.log 2>&1 < /dev/null &
