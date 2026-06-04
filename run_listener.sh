#!/bin/bash
# Запуск слушателя /connect (привязка Telegram) с полным detach через setsid,
# чтобы процесс переживал завершение родителя (cron-reap / exec-teardown).
# Вынесен в отдельный файл, чтобы строка «tg_connect_bot.py» не попадала в
# командную строку watchdog'а (иначе pgrep матчил бы сам себя).
HH_DB_SCHEMA=u_egor setsid /usr/local/bin/python -u /app/tg_connect_bot.py \
  >> /var/log/cron.log 2>&1 < /dev/null &
