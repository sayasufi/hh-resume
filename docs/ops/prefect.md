# Оркестрация на Prefect — рунбук

Периодические задачи hh-applicant-tool оркестрируются **Prefect 3** (self-hosted),
заменив cron. Всё крутится в docker-compose на сервере (`/var/www1/hh-applicant-tool`).

## Сервисы (docker compose)

| Сервис | Роль | Порт |
|---|---|---|
| `db` (hh_db) | Postgres 16 — данные приложения + БД `prefect` (метаданные) | внутр. 5432 |
| `prefect-server` | Prefect API + UI (дашборд) | **0.0.0.0:4200** (наружу) |
| `hh-orchestrator` | `python -m orchestration.serve` — обслуживает 11 деплойментов по расписанию | — |
| `hh-web` | Mini App бэкенд (uvicorn `services.web_app:app`) | 127.0.0.1:60080 (наружу через nginx) |
| `hh-listener` | Telegram /connect бот (`services/tg_connect_bot.py`) | — |

vLLM — внешний стек (`test-llm_default` сеть), НЕ трогаем.

## Дашборд

Открыть: **http://109.120.183.147:4200** → basic-auth `admin` / пароль из `.env`
(`PREFECT_AUTH=admin:...`, файл gitignored, не в репозитории).

В UI: все 11 деплойментов (`dispatch/hh-<job>`), история ранов, статусы, логи по каждому
рану и аккаунту, расписания, ближайшие запуски.

## Задачи (источник правды — `orchestration/flows.py`, таблица `JOBS`)

Один параметризованный флоу `dispatch(job_name)` на расписании читает активные цели
(`active_targets(platform, feature)` — для hh это `app_users` с включённой фичей) и
раздаёт per-target задачи, которые запускают существующую операцию как subprocess.

Деплойменты: `dispatch/hh-refresh-token`, `…/hh-apply-similar`, `…/hh-reply-employers`,
`…/hh-giga`, `…/hh-apply-tests`, `…/hh-notify-actions`, `…/hh-send-digest`,
`…/hh-browse-activity`, `…/hh-update-resumes`, `…/hh-monitor`, `…/hh-funnel`.

## Частые операции

```bash
cd /var/www1/hh-applicant-tool
docker compose ps                                   # статус стека
docker logs -f hh_orchestrator                      # лог исполнения задач
docker exec hh_orchestrator prefect deployment ls   # список деплойментов
docker exec hh_orchestrator prefect deployment run "dispatch/hh-funnel"   # запустить вручную
docker exec hh_orchestrator prefect flow-run ls --limit 10                # последние раны
```

**Добавить задачу:** добавить строку в `JOBS` (`orchestration/flows.py`), затем
`docker compose restart hh-orchestrator`. `ORCH_ENABLED` в compose (`hh-orchestrator.environment`)
— список обслуживаемых задач (`all` = все); правка + `docker compose up -d hh-orchestrator`.

**Поставить на паузу / снять:**
```bash
docker exec hh_orchestrator prefect deployment pause "dispatch/hh-apply-similar"
docker exec hh_orchestrator prefect deployment resume "dispatch/hh-apply-similar"
```

**Включить/выключить per-аккаунт** — тумблеры в кабинете (feat.*), читаются каждым раном.

## Надёжность

- **Ретраи** задач: 2 попытки с backoff (в `run_target`).
- **Лимиты конкуренции** (защита vLLM): tag `llm`=3, `browser`=1.
  `docker exec hh_orchestrator prefect concurrency-limit ls`.
- **giga** — per-account single-flight через Postgres advisory-лок (внутри `giga_recruiter.py`).
- **Алерты падений** → Telegram: хук `on_failure` кладёт 🔴-уведомление, доставит `send-digest`.
- **Сервисы** (web/listener) — `restart: always`.

## Откат отдельной задачи

Если задача глючит: `prefect deployment pause "dispatch/hh-<job>"`. cron удалён, поэтому
полный откат на cron невозможен — раны управляются через Prefect (pause/resume/manual run).

## Бэкап

- БД приложения + метаданные Prefect — в одном Postgres (`db`), volume `pgdata`.
  Бэкап: `docker exec hh_db pg_dump -U hh hh` и `... prefect`.
- Снимок репозитория делался при миграции: `/var/www1/hh-bak-*.tar.gz`.

## Что НЕ в репозитории

- `config/` (секреты hh/telegram, сессии) — gitignored, состояние в Postgres.
- `.env` (`PREFECT_AUTH`) — gitignored.
