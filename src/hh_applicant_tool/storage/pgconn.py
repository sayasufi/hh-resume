"""Подключение к Postgres со схемой-на-юзера (search_path).

DSN из env HH_DB_DSN, схема из HH_DB_SCHEMA (по умолчанию public).
connect() само-провижинит схему и таблицы (идемпотентно), так что добавить
нового юзера = просто задать HH_DB_SCHEMA и запустить.
"""
from __future__ import annotations

import os

import psycopg

# Таблицы внутри текущей схемы (search_path уже выставлен). Идемпотентно.
TABLES_DDL = """
CREATE TABLE IF NOT EXISTS employers (
    id bigint PRIMARY KEY, name text NOT NULL, type text, description text,
    site_url text, area_id bigint, area_name text, alternate_url text,
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS vacancy_contacts (
    id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    vacancy_id bigint NOT NULL, vacancy_alternate_url text, vacancy_name text,
    vacancy_area_id bigint, vacancy_area_name text, vacancy_salary_from bigint,
    vacancy_salary_to bigint, vacancy_currency varchar(3), vacancy_gross boolean,
    employer_id bigint, employer_name text, name text, email text,
    phone_numbers text NOT NULL,
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
    UNIQUE (vacancy_id, email)
);
CREATE TABLE IF NOT EXISTS vacancies (
    id bigint PRIMARY KEY, name text NOT NULL, area_id bigint, area_name text,
    salary_from bigint, salary_to bigint, currency varchar(3), gross boolean,
    published_at timestamptz, created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(), remote boolean, experience text,
    professional_roles text, alternate_url text
);
CREATE TABLE IF NOT EXISTS negotiations (
    id bigint PRIMARY KEY, state text NOT NULL, vacancy_id bigint NOT NULL,
    employer_id bigint, chat_id bigint NOT NULL, resume_id text,
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS settings (key text PRIMARY KEY, value text NOT NULL);
CREATE TABLE IF NOT EXISTS resumes (
    id text PRIMARY KEY, title text NOT NULL, url text, alternate_url text,
    status_id text, status_name text, can_publish_or_update boolean,
    total_views integer DEFAULT 0, new_views integer DEFAULT 0,
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app_config (
    key text PRIMARY KEY, value jsonb NOT NULL, updated_at timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS seen_keys (
    kind text NOT NULL, key text NOT NULL, created_at timestamptz DEFAULT now(),
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS action_items (
    id bigserial PRIMARY KEY, nid bigint, chat_id bigint, vacancy text,
    action text NOT NULL, chat_url text, vacancy_url text,
    created_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_vac_upd ON vacancies(updated_at);
CREATE INDEX IF NOT EXISTS idx_emp_upd ON employers(updated_at);
CREATE INDEX IF NOT EXISTS idx_neg_upd ON negotiations(updated_at);
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $func$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$func$ LANGUAGE plpgsql;
DO $do$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['employers','vacancy_contacts','vacancies','negotiations','resumes']
  LOOP
    -- CREATE OR REPLACE (PG14+) вместо DROP+CREATE: без ACCESS EXCLUSIVE churn
    EXECUTE format(
      'CREATE OR REPLACE TRIGGER trg_%1$s_updated BEFORE UPDATE ON %1$s
       FOR EACH ROW EXECUTE FUNCTION set_updated_at();', t);
  END LOOP;
END $do$;
"""


def get_schema() -> str:
    return os.environ.get("HH_DB_SCHEMA", "public")


def get_dsn() -> str:
    dsn = os.environ.get("HH_DB_DSN")
    if not dsn:
        raise RuntimeError("HH_DB_DSN не задан")
    return dsn


def connect(ensure: bool = False) -> psycopg.Connection:
    """Sync-соединение (для Config) с search_path на схему юзера.
    ensure=True — создать схему/таблицы (DDL). По умолчанию ensure=False:
    схема провижинится один раз (startup/register_user), а горячий путь
    (get_setting/Config.load/save) НЕ гоняет DDL на каждый коннект."""
    schema = get_schema()
    conn = psycopg.connect(get_dsn())
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
        if ensure:
            cur.execute(TABLES_DDL)
    conn.commit()
    return conn


async def aconnect(ensure: bool = False) -> psycopg.AsyncConnection:
    """Async-соединение (для storage) с search_path на схему юзера.
    ensure=False по умолчанию — DDL провижинится отдельно (см. connect)."""
    schema = get_schema()
    conn = await psycopg.AsyncConnection.connect(get_dsn())
    async with conn.cursor() as cur:
        await cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        await cur.execute(f'SET search_path TO "{schema}"')
        if ensure:
            await cur.execute(TABLES_DDL)
    await conn.commit()
    return conn


# --- Sync-хелперы для standalone-скриптов (apply_tests, notify_actions) ---

def app_config() -> dict:
    """Весь app_config (token/openai/telegram/preferences/...) как dict."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM app_config")
            return {k: v for k, v in cur.fetchall()}
    finally:
        conn.close()


def set_app_config(key: str, value) -> None:
    """Записать/обновить ключ в app_config (jsonb) текущей схемы."""
    import json as _json

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_config(key, value) VALUES (%s, %s::jsonb) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = now()",
                (key, _json.dumps(value, ensure_ascii=False)),
            )
        conn.commit()
    finally:
        conn.close()


def list_users() -> list[tuple[str, str]]:
    """Активные юзеры из public.app_users -> [(name, schema), ...].
    Таблица создаётся при отсутствии (идемпотентно)."""
    conn = psycopg.connect(get_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS public.app_users ("
                "id serial PRIMARY KEY, name text UNIQUE, "
                "schema text UNIQUE NOT NULL, active boolean DEFAULT true, "
                "created_at timestamptz DEFAULT now())"
            )
            conn.commit()
            cur.execute(
                "SELECT name, schema FROM public.app_users "
                "WHERE active ORDER BY id"
            )
            return cur.fetchall()
    finally:
        conn.close()


def register_user(name: str, schema: str) -> None:
    conn = psycopg.connect(get_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS public.app_users ("
                "id serial PRIMARY KEY, name text UNIQUE, "
                "schema text UNIQUE NOT NULL, active boolean DEFAULT true, "
                "created_at timestamptz DEFAULT now())"
            )
            cur.execute(
                "INSERT INTO public.app_users(name, schema) VALUES (%s, %s) "
                "ON CONFLICT(name) DO UPDATE SET schema = excluded.schema, "
                "active = true",
                (name, schema),
            )
        conn.commit()
    finally:
        conn.close()


def get_setting(key: str, default=None):
    """JSON-декодированное значение из settings (как хранит SettingModel)."""
    import json as _json

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return default
    try:
        return _json.loads(row[0])
    except (ValueError, TypeError):
        return row[0]


def seen_keys(kind: str) -> set:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key FROM seen_keys WHERE kind = %s", (kind,))
            return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def add_seen(kind: str, keys) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            for key in keys:
                cur.execute(
                    "INSERT INTO seen_keys(kind, key) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (kind, str(key)),
                )
        conn.commit()
    finally:
        conn.close()


def add_action_items(items: list[dict]) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            for it in items:
                cur.execute(
                    "INSERT INTO action_items(nid, chat_id, vacancy, action, "
                    "chat_url, vacancy_url) VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        it.get("nid"),
                        it.get("chat_id"),
                        it.get("vacancy"),
                        it.get("action"),
                        it.get("chat_url"),
                        it.get("vacancy_url"),
                    ),
                )
        conn.commit()
    finally:
        conn.close()
