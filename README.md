# hh-applicant-tool (multi-user, async, Postgres)

Автоматизация поиска работы на hh.ru для **нескольких пользователей в одном
контейнере**. Форк [s3rgeym/hh-applicant-tool](https://github.com/s3rgeym/hh-applicant-tool),
переписанный на **async** (httpx + psycopg) с хранением всего состояния в
**Postgres** (схема на пользователя).

## Что делает (по расписанию, для каждого активного юзера)

- **apply-similar** — отклики на подходящие вакансии; сопроводительные письма
  генерирует локальная LLM (vLLM), заземление на резюме.
- **apply_tests.py** — отклик на вакансии **с тестом** через браузер (Playwright):
  текст/radio/checkbox/микс.
- **reply-employers** — авто-ответы в диалогах с работодателями (по резюме,
  без выдумок дат/зарплат/имён).
- **notify_actions.py** — извлекает «дела» из диалогов (тест/анкета/интервью/
  написать в ТГ) и шлёт в Telegram-группу.
- **update-resumes** — подъём резюме; **refresh-token** — обновление OAuth.

## Архитектура

- **Один контейнер** обслуживает всех. `run_all.py` читает активных юзеров из
  `public.app_users` и запускает команду для каждого со своей `HH_DB_SCHEMA`.
- **Postgres**, схема на юзера (`u_<name>`): `app_config` (token/openai/telegram/
  preferences/**resume_text**/**web_state**), `settings`, `seen_keys`,
  `action_items`, кэш вакансий/негоциаций.
- Реестр юзеров: `public.app_users (name, schema, active)`.

## Запуск

```bash
docker compose up -d --build      # поднимает Postgres (db) + контейнер с cron
```

Переменные контейнера: `HH_DB_DSN` (Postgres), `HH_DB_SCHEMA` (по умолчанию
`public`; реальную схему выставляет `run_all.py` на каждого юзера).

### Добавить пользователя

1. OAuth (получить токен hh) и записать в схему `u_<name>` (`app_config.token`).
2. Зарегистрировать: `INSERT INTO public.app_users(name, schema) VALUES (...)`.
3. Заполнить `app_config.resume_text`, `settings.apply.resume_id` и т.п.

Миграция данных из старого SQLite/JSON: `pg/migrate_user.py`
(env `HH_DB_SCHEMA`, `MIGRATE_USER_NAME`, `MIGRATE_CONFIG_DIR`).

## Ручной запуск команды для всех

```bash
python run_all.py -- python -m hh_applicant_tool whoami
python run_all.py -- python apply_tests.py --apply --limit 10
```
