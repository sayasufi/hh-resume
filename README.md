# hh-applicant-tool (multi-user, async, Postgres, Prefect)

Автоматизация поиска работы на hh.ru для **нескольких кандидатов в одном стеке**:
авто-отклики, авто-ответы работодателям, прохождение тест-вакансий и интервью
ГигаРекрутера, Telegram-уведомления и Mini App «личный кабинет».

Async (httpx + psycopg), всё состояние в **Postgres** (одна схема `public`, разделение
по колонке `account`). Периодические задачи оркестрирует **Prefect 3** (заменил cron).

## Что делает (по расписанию, для каждого активного аккаунта)

- **apply-similar** — отклики на подходящие вакансии; сопроводительные письма от
  локальной LLM (vLLM), заземление на резюме; фильтр «только ГПХ».
- **apply-tests** (`services/apply_tests.py`) — отклик на вакансии **с тестом** через
  браузер (Playwright).
- **reply-employers** — авто-ответы в диалогах (по резюме, без выдумок дат/зарплат/имён;
  не берёт на себя внешние задания).
- **giga** (`services/giga_recruiter.py`) — авто-прохождение интервью ГигаРекрутера в
  Telegram через Telethon-сессию кандидата.
- **notify-actions** + **send-digest** — «дела» из диалогов → приоритизированный дайджест
  в личку кандидату.
- **browse-activity**, **update-resumes**, **refresh-token**, **monitor**, **funnel** —
  человекоподобная активность, подъём резюме, OAuth, мониторинг, воронка.

## Архитектура

Стек docker-compose (см. `docs/ops/prefect.md`):

| Сервис | Роль |
|---|---|
| `db` | Postgres: данные приложения + БД `prefect` (метаданные оркестратора) |
| `prefect-server` + `prefect-proxy` | Prefect API/UI (дашборд) за nginx basic-auth |
| `hh-orchestrator` | Prefect `serve()` — 11 задач по расписанию, fan-out по аккаунтам |
| `hh-web` | Mini App бэкенд (FastAPI/uvicorn, `services/web_app.py`) |
| `hh-listener` | Telegram /connect бот (`services/tg_connect_bot.py`) |

- **Postgres**, схема `public`, разделение по колонке `account`: `app_config`
  (token/openai/telegram/preferences/**resume_text**/**web_state**/сессии), `settings`,
  `seen_keys`, `action_items`, `notifications`, `giga_queue`, `activity_daily`, кэш.
- Реестр аккаунтов: `public.app_users (name, account, active)`.
- Оркестрация: один параметризованный флоу `dispatch(job_name)` (`orchestration/flows.py`,
  таблица `JOBS`) читает активные цели и запускает операцию как subprocess per-аккаунт.
  Заложено платформо-обобщённо (`active_targets(platform, …)`) под будущие источники
  (Habr/GetMatch/Telegram-каналы) — см. `docs/superpowers/specs/`.

## Структура репозитория

```
src/hh_applicant_tool/   ядро-пакет (CLI-операции: apply-similar, reply-employers, …)
orchestration/           Prefect-флоу (flows/serve/targets/runner/jitter/alerts)
services/                entrypoint-скрипты (web_app, tg_connect_bot, giga_recruiter,
                         apply_tests, browse_activity, notify_actions, send_digest,
                         monitor, funnel, onboard)
webapp_static/           фронтенд Mini App
nginx/                   конфиг прокси для Prefect UI
pg/ tests/ docs/         SQL, тесты, документация
```

## Запуск

```bash
docker compose up -d            # поднимает весь стек
```

Дашборд оркестрации: **http://<server-ip>:4200** (basic-auth, см. `docs/ops/prefect.md`).
Mini App: `hh-web` на 127.0.0.1:60080 (наружу — через nginx+TLS на хосте).

## Добавить аккаунт

Через Telegram-бот (`/addaccount` — OAuth + онбординг) или вручную:
1. Получить токен hh (OAuth) → `app_config(account, key='token')`.
2. `INSERT INTO public.app_users(name, account) VALUES (...)`.
3. Заполнить `app_config.resume_text`, `settings.apply.resume_id`, тумблеры `feat.*`.

## Операции вручную

```bash
docker exec hh_orchestrator prefect deployment run "dispatch/hh-funnel"   # запустить задачу
docker exec -e HH_ACCOUNT=<acc> hh_web python -m hh_applicant_tool whoami # разовая команда
```

Подробности эксплуатации, откат, бэкап — в **`docs/ops/prefect.md`**.
