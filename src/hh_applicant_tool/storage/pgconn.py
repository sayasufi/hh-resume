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
CREATE TABLE IF NOT EXISTS notifications (
    id bigserial PRIMARY KEY, priority int NOT NULL DEFAULT 2,
    category text, text text NOT NULL, link text, dedup_key text UNIQUE,
    created_at timestamptz DEFAULT now(), sent_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_notif_unsent
    ON notifications(sent_at, priority, created_at);
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
        # CREATE SCHEMA — только при ensure (провижин админом). Tenant-роли (#18)
        # имеют лишь USAGE на свою схему, без CREATE: горячий путь не создаёт схему.
        if ensure:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
        if ensure:
            cur.execute(TABLES_DDL)
    conn.commit()
    return conn


async def locked_token_refresh(api_client) -> bool:
    """Обновление OAuth-токена под advisory-lock (защита от гонки одновременных
    refresh: cron-refresh каждую минуту vs 403-refresh внутри долгой команды).

    HH ротирует refresh_token (часто одноразовый) — два параллельных refresh с
    одним и тем же refresh_token разлогинят юзера. Здесь: берём
    pg_advisory_xact_lock на схему; перечитываем токен из PG — если другой
    процесс уже обновил (валиден) — принимаем его БЕЗ повторного refresh; иначе
    обновляем через HH и сохраняем. Всё в одной транзакции (lock снимается на
    commit). Возвращает True при успехе."""
    import json as _json
    import time as _time

    schema = get_schema()
    conn = await psycopg.AsyncConnection.connect(get_dsn())
    try:
        async with conn.cursor() as cur:
            await cur.execute(f'SET search_path TO "{schema}"')
            # сериализуем refresh по юзеру (блокирует конкурентов до commit)
            await cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (schema + ":token",)
            )
            await cur.execute(
                "SELECT value FROM app_config WHERE key = 'token'"
            )
            row = await cur.fetchone()
            pg_tok = row[0] if row else None
            # кто-то уже обновил, пока мы ждали лок — принимаем его токен
            if pg_tok and pg_tok.get("access_expires_at", 0) > _time.time() + 30:
                api_client.handle_access_token(pg_tok)
                await conn.commit()
                # PG уже содержит актуальный токен — save_token не нужен (#7)
                api_client._token_persisted = True
                return True
            # всё ещё истёк — реально обновляем через HH под локом
            new = await api_client.oauth_client.refresh_access_token(
                api_client.refresh_token
            )
            api_client.handle_access_token(new)
            await cur.execute(
                "INSERT INTO app_config(key, value) VALUES ('token', %s::jsonb) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = now()",
                (_json.dumps(new, ensure_ascii=False),),
            )
            await conn.commit()
            # токен записан в PG под локом — внешний save_token избыточен (#7)
            api_client._token_persisted = True
            return True
    finally:
        await conn.close()


async def aconnect(ensure: bool = False) -> psycopg.AsyncConnection:
    """Async-соединение (для storage) с search_path на схему юзера.
    ensure=False по умолчанию — DDL провижинится отдельно (см. connect)."""
    schema = get_schema()
    conn = await psycopg.AsyncConnection.connect(get_dsn())
    async with conn.cursor() as cur:
        if ensure:
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


def _ensure_app_users(cur) -> None:
    """Идемпотентно создаёт public.app_users и колонку db_password (#18).
    db_password — пароль tenant-роли (login-роль на схему); читать может только
    admin-роль hh (tenant-ролям SELECT на app_users не выдаётся)."""
    cur.execute(
        "CREATE TABLE IF NOT EXISTS public.app_users ("
        "id serial PRIMARY KEY, name text UNIQUE, "
        "schema text UNIQUE NOT NULL, active boolean DEFAULT true, "
        "created_at timestamptz DEFAULT now())"
    )
    cur.execute(
        "ALTER TABLE public.app_users ADD COLUMN IF NOT EXISTS db_password text"
    )


def list_users() -> list[tuple[str, str]]:
    """Активные юзеры из public.app_users -> [(name, schema), ...]."""
    conn = psycopg.connect(get_dsn())
    try:
        with conn.cursor() as cur:
            _ensure_app_users(cur)
            conn.commit()
            cur.execute(
                "SELECT name, schema FROM public.app_users "
                "WHERE active ORDER BY id"
            )
            return cur.fetchall()
    finally:
        conn.close()


def list_users_full() -> list[tuple[str, str, str | None]]:
    """Активные юзеры -> [(name, schema, db_password), ...] для run_all (#18).
    db_password=None → run_all использует admin-DSN (фолбэк/откат)."""
    conn = psycopg.connect(get_dsn())
    try:
        with conn.cursor() as cur:
            _ensure_app_users(cur)
            conn.commit()
            cur.execute(
                "SELECT name, schema, db_password FROM public.app_users "
                "WHERE active ORDER BY id"
            )
            return cur.fetchall()
    finally:
        conn.close()


def tenant_dsn(admin_dsn: str, role: str, password: str) -> str:
    """DSN tenant-роли: берём host/port/dbname из admin-DSN, меняем user+password.
    Имя роли = имя схемы (u_egor/u_lexa)."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    d = conninfo_to_dict(admin_dsn)
    d["user"] = role
    d["password"] = password
    return make_conninfo(**d)


def register_user(name: str, schema: str) -> None:
    conn = psycopg.connect(get_dsn())
    try:
        with conn.cursor() as cur:
            _ensure_app_users(cur)
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


# --- Единые приоритизированные уведомления (TG-дайджест) ---
# Приоритет: меньше = важнее. Дайджест сортирует по нему.
PRIORITY_HIGH = 1   # 🔴 нужен ты лично/срочно
PRIORITY_MED = 2    # 🟡 действие, не срочно
PRIORITY_LOW = 3    # 🟢 рутина/инфо


def _norm_phone(p) -> str:
    """Последние 10 цифр номера (без кода страны/плюса) — для сопоставления
    Telegram-номера с номером hh-профиля."""
    d = "".join(ch for ch in str(p or "") if ch.isdigit())
    return d[-10:]


def _session_key() -> bytes:
    """Ключ шифрования Telegram-сессий. Из env HH_SESSION_KEY либо файл
    <CONFIG_DIR>/.session_key (генерится 1 раз, не в git/образе, в bind-mount)."""
    from cryptography.fernet import Fernet

    k = os.environ.get("HH_SESSION_KEY")
    if k:
        return k.encode()
    path = os.path.join(os.environ.get("CONFIG_DIR", "/app/config"), ".session_key")
    try:
        with open(path, "rb") as f:
            return f.read().strip()
    except FileNotFoundError:
        key = Fernet.generate_key()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(key)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return key


def tg_api() -> tuple[int, str]:
    """(api_id, api_hash) для Telethon. Источник: файл <CONFIG_DIR>/.tg_api (json
    {"api_id":..., "api_hash":...}) -> env HH_TG_API_ID/HH_TG_API_HASH -> публичный
    дефолт (Telegram Desktop). Своё приложение чище по ToS, чем общий ключ."""
    import json as _json

    path = os.path.join(os.environ.get("CONFIG_DIR", "/app/config"), ".tg_api")
    try:
        with open(path) as f:
            d = _json.load(f)
            return int(d["api_id"]), str(d["api_hash"])
    except Exception:
        pass
    aid = os.environ.get("HH_TG_API_ID")
    if aid:
        return int(aid), os.environ.get("HH_TG_API_HASH", "")
    return 2040, "b18441a1ff607e10a989891a5462e627"


def enc_session(s: str) -> str:
    from cryptography.fernet import Fernet

    return Fernet(_session_key()).encrypt(s.encode()).decode()


def dec_session(s: str) -> str:
    from cryptography.fernet import Fernet

    try:
        return Fernet(_session_key()).decrypt(s.encode()).decode()
    except Exception:
        return s  # не зашифровано/иной формат — вернуть как есть


def notify(
    priority: int,
    text: str,
    category: str | None = None,
    link: str | None = None,
    dedup_key: str | None = None,
) -> None:
    """Положить уведомление в очередь (таблица notifications текущей схемы).
    Отправит позже send_digest.py одним отсортированным дайджестом.
    dedup_key (если задан) защищает от повторов одного и того же события
    между прогонами. Если dedup_key=None — дубль-защиты нет (NULL уникальны в PG)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notifications(priority, category, text, link, dedup_key) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (dedup_key) DO NOTHING",
                (priority, category, text, link, dedup_key),
            )
        conn.commit()
    finally:
        conn.close()
