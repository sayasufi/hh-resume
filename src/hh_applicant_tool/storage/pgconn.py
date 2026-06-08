"""Postgres: ОДНА общая схема (public). Строки разделяются колонкой `account`
(имя аккаунта из env HH_ACCOUNT; run_all задаёт его per-аккаунт). DSN из HH_DB_DSN.

Per-account таблицы (с колонкой account): app_config, settings, seen_keys,
action_items, notifications. Кэш-таблицы (employers, vacancy_contacts, vacancies,
negotiations, resumes) — ОБЩИЕ (PK = hh-глобальные id, между аккаунтами не
пересекаются), без account. Реестр аккаунтов — public.app_users(name, account).
"""
from __future__ import annotations

import os

import psycopg

from . import _cfgmap as _M  # единый маппинг legacy-ключей -> нормализованные таблицы


def _users_set(cur, acc, col, value, jsonb=False):
    """Upsert одной колонки users (создаёт строку юзера при необходимости)."""
    import json as _json
    val = _json.dumps(value, ensure_ascii=False) if (jsonb and value is not None) else value
    cast = "::jsonb" if jsonb else ""
    cur.execute(
        f"INSERT INTO users(account, {col}) VALUES (%s, %s{cast}) "
        f"ON CONFLICT(account) DO UPDATE SET {col}=excluded.{col}, updated_at=now()",
        (acc, val),
    )

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
CREATE TABLE IF NOT EXISTS resumes (
    id text PRIMARY KEY, title text NOT NULL, url text, alternate_url text,
    status_id text, status_name text, can_publish_or_update boolean,
    total_views integer DEFAULT 0, new_views integer DEFAULT 0,
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now()
);
-- per-account
CREATE TABLE IF NOT EXISTS settings (
    account text NOT NULL DEFAULT '', key text NOT NULL, value text NOT NULL,
    PRIMARY KEY (account, key)
);
CREATE TABLE IF NOT EXISTS seen_keys (
    account text NOT NULL DEFAULT '', kind text NOT NULL, key text NOT NULL,
    created_at timestamptz DEFAULT now(), PRIMARY KEY (account, kind, key)
);
CREATE TABLE IF NOT EXISTS action_items (
    id bigserial PRIMARY KEY, account text NOT NULL DEFAULT '',
    nid bigint, chat_id bigint, vacancy text, action text NOT NULL,
    chat_url text, vacancy_url text, created_at timestamptz DEFAULT now(),
    done boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS notifications (
    id bigserial PRIMARY KEY, account text NOT NULL DEFAULT '',
    priority int NOT NULL DEFAULT 2, category text, text text NOT NULL,
    link text, dedup_key text, created_at timestamptz DEFAULT now(),
    sent_at timestamptz, UNIQUE (account, dedup_key)
);
CREATE TABLE IF NOT EXISTS activity_daily (
    account text NOT NULL, day date NOT NULL, kind text NOT NULL,
    count int NOT NULL DEFAULT 0, PRIMARY KEY (account, day, kind)
);
CREATE TABLE IF NOT EXISTS or_usage (
    day date PRIMARY KEY, count int NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS giga_queue (
    account text NOT NULL, token text NOT NULL, vacancy text, nid bigint,
    status text NOT NULL DEFAULT 'pending', turns int NOT NULL DEFAULT 0,
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
    PRIMARY KEY (account, token)
);
CREATE INDEX IF NOT EXISTS idx_notif_unsent
    ON notifications(account, sent_at, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_vac_upd ON vacancies(updated_at);
CREATE INDEX IF NOT EXISTS idx_emp_upd ON employers(updated_at);
CREATE INDEX IF NOT EXISTS idx_neg_upd ON negotiations(updated_at);
CREATE TABLE IF NOT EXISTS app_users (
    id serial PRIMARY KEY, name text, account text UNIQUE NOT NULL,
    active boolean DEFAULT true, created_at timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS users (
    account text PRIMARY KEY,
    name text, full_name text, email text, phone text, hh_phone text, active boolean DEFAULT true,
    hh_token jsonb, openai jsonb, telegram jsonb, preferences jsonb,
    resume_text text, tg_user_id bigint, tg_user_session text,
    getmatch_session text, getmatch_username text, getmatch_max_per_day int,
    habr_login text, habr_password text, habr_session text,
    habr_2captcha_key text, habr_query text, habr_max_per_day int,
    auth_username text, auth_password text, auth_last_login bigint,
    apply_resume_id text, apply_max_per_day int, apply_tests_per_day int,
    apply_use_ai boolean, apply_force_message boolean,
    apply_civil_law_only boolean, apply_excluded_terms text,
    applications_count text, applications_date text, applications_pause_until text,
    tg_cats text, reply_ignore_names text,
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS user_features (
    account text, feature text, enabled boolean DEFAULT false,
    PRIMARY KEY (account, feature)
);
CREATE TABLE IF NOT EXISTS health (
    account text, feature text, ts bigint, ok boolean, detail text,
    PRIMARY KEY (account, feature)
);
CREATE TABLE IF NOT EXISTS web_state (
    account text PRIMARY KEY, state jsonb, updated_at timestamptz DEFAULT now()
);
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $func$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$func$ LANGUAGE plpgsql;
DO $do$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['employers','vacancy_contacts','vacancies','negotiations','resumes','users','web_state']
  LOOP
    EXECUTE format(
      'CREATE OR REPLACE TRIGGER trg_%1$s_updated BEFORE UPDATE ON %1$s
       FOR EACH ROW EXECUTE FUNCTION set_updated_at();', t);
  END LOOP;
END $do$;
"""


def get_account() -> str:
    """Имя аккаунта (раздел данных). Из HH_ACCOUNT; фолбэк — старый HH_DB_SCHEMA
    (со снятым префиксом u_) для обратной совместимости."""
    acc = os.environ.get("HH_ACCOUNT")
    if acc:
        return acc
    s = os.environ.get("HH_DB_SCHEMA", "")
    return s[2:] if s.startswith("u_") else (s or "default")


def get_schema() -> str:  # back-compat: теперь = account
    return get_account()


def get_dsn() -> str:
    dsn = os.environ.get("HH_DB_DSN")
    if not dsn:
        raise RuntimeError("HH_DB_DSN не задан")
    return dsn


def connect(ensure: bool = False) -> psycopg.Connection:
    conn = psycopg.connect(get_dsn())
    with conn.cursor() as cur:
        cur.execute("SET search_path TO public")
        if ensure:
            cur.execute(TABLES_DDL)
    conn.commit()
    return conn


async def aconnect(ensure: bool = False) -> psycopg.AsyncConnection:
    conn = await psycopg.AsyncConnection.connect(get_dsn())
    async with conn.cursor() as cur:
        await cur.execute("SET search_path TO public")
        if ensure:
            await cur.execute(TABLES_DDL)
    await conn.commit()
    return conn


async def locked_token_refresh(api_client) -> bool:
    """Обновление OAuth-токена под advisory-lock (по аккаунту). См. историю #4."""
    import json as _json
    import time as _time

    acc = get_account()
    conn = await psycopg.AsyncConnection.connect(get_dsn())
    try:
        async with conn.cursor() as cur:
            await cur.execute("SET search_path TO public")
            await cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (acc + ":token",)
            )
            await cur.execute(
                "SELECT hh_token FROM users WHERE account=%s", (acc,)
            )
            row = await cur.fetchone()
            pg_tok = row[0] if row else None
            if pg_tok and pg_tok.get("access_expires_at", 0) > _time.time() + 30:
                api_client.handle_access_token(pg_tok)
                await conn.commit()
                api_client._token_persisted = True
                return True
            new = await api_client.oauth_client.refresh_access_token(
                api_client.refresh_token
            )
            api_client.handle_access_token(new)
            await cur.execute(
                "INSERT INTO users(account, hh_token) VALUES (%s, %s::jsonb) "
                "ON CONFLICT(account) DO UPDATE SET hh_token=excluded.hh_token, updated_at=now()",
                (acc, _json.dumps(new, ensure_ascii=False)),
            )
            await conn.commit()
            api_client._token_persisted = True
            return True
    finally:
        await conn.close()


# --- Sync-хелперы (account-aware) ---

def app_config(account: str | None = None) -> dict:
    acc = account or get_account()
    out: dict = {}
    conn = connect()
    try:
        with conn.cursor() as cur:
            cols = [c for _, c in _M.APP_ORDER]
            cur.execute(f"SELECT {', '.join(cols)} FROM users WHERE account=%s", (acc,))
            row = cur.fetchone()
            if row:
                for (k, _c), v in zip(_M.APP_ORDER, row):
                    if v is not None:
                        out[k] = v
            cur.execute("SELECT state FROM web_state WHERE account=%s", (acc,))
            ws = cur.fetchone()
            if ws and ws[0] is not None:
                out["web_state"] = ws[0]
    finally:
        conn.close()
    return out


def set_app_config(key: str, value, account: str | None = None) -> None:
    import json as _json
    acc = account or get_account()
    conn = connect()
    try:
        with conn.cursor() as cur:
            if key == "web_state":
                cur.execute(
                    "INSERT INTO web_state(account, state) VALUES (%s, %s::jsonb) "
                    "ON CONFLICT(account) DO UPDATE SET state=excluded.state, updated_at=now()",
                    (acc, _json.dumps(value, ensure_ascii=False)),
                )
            elif key in _M.APP_COL:
                col = _M.APP_COL[key]
                _users_set(cur, acc, col, _M.coerce_user(col, value), jsonb=(col in _M.APP_JSONB))
            else:  # незамапленный app_config-ключ — добавь его в _cfgmap.APP_COL (+ колонку users)
                raise ValueError(
                    f"set_app_config: незамапленный ключ {key!r} — добавьте в _cfgmap.APP_COL "
                    "и колонку в users (таблица app_config удалена)"
                )
        conn.commit()
    finally:
        conn.close()


def list_users() -> list[tuple[str, str]]:
    """Активные аккаунты -> [(name, account), ...]."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, account FROM app_users WHERE active ORDER BY id"
            )
            return cur.fetchall()
    finally:
        conn.close()


def register_user(name: str, account: str) -> None:
    conn = connect(ensure=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_users(name, account) VALUES (%s, %s) "
                "ON CONFLICT(account) DO UPDATE SET name=excluded.name, active=true",
                (name, account),
            )
        conn.commit()
    finally:
        conn.close()


def get_setting(key: str, default=None, account: str | None = None):
    import json as _json
    acc = account or get_account()
    kind = _M.resolve_setting(key)
    conn = connect()
    try:
        with conn.cursor() as cur:
            if kind[0] == "feature":
                cur.execute("SELECT enabled FROM user_features WHERE account=%s AND feature=%s", (acc, kind[1]))
                r = cur.fetchone()
                return bool(r[0]) if r else default
            if kind[0] == "health":
                cur.execute("SELECT ts, ok, detail FROM health WHERE account=%s AND feature=%s", (acc, kind[1]))
                r = cur.fetchone()
                return {"ts": r[0], "ok": r[1], "detail": r[2]} if r else default
            if kind[0] == "users":
                cur.execute(f"SELECT {kind[1]} FROM users WHERE account=%s", (acc,))
                r = cur.fetchone()
                return r[0] if (r and r[0] is not None) else default
            cur.execute("SELECT value FROM settings WHERE account=%s AND key=%s", (acc, key))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return default
    try:
        return _json.loads(row[0])
    except (ValueError, TypeError):
        return row[0]


def set_setting(key: str, value, account: str | None = None) -> None:
    """Запись настройки в нормализованную таблицу по маппингу (_cfgmap).
    feat.* -> user_features, _health.* -> health, замапленные -> users,
    глобальные/прочие -> settings (json-кодированно, как читает get_setting)."""
    import json as _json
    acc = account or get_account()
    kind = _M.resolve_setting(key)
    conn = connect()
    try:
        with conn.cursor() as cur:
            if kind[0] == "feature":
                cur.execute(
                    "INSERT INTO user_features(account, feature, enabled) VALUES (%s, %s, %s) "
                    "ON CONFLICT(account, feature) DO UPDATE SET enabled=excluded.enabled",
                    (acc, kind[1], bool(value)),
                )
            elif kind[0] == "health":
                v = value if isinstance(value, dict) else {}
                cur.execute(
                    "INSERT INTO health(account, feature, ts, ok, detail) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT(account, feature) DO UPDATE SET ts=excluded.ts, ok=excluded.ok, detail=excluded.detail",
                    (acc, kind[1], v.get("ts"), v.get("ok"), str(v.get("detail") or "")[:300]),
                )
            elif kind[0] == "users":
                col = kind[1]
                _users_set(cur, acc, col, _M.coerce_user(col, value), jsonb=(col in _M.APP_JSONB))
            else:
                cur.execute(
                    "INSERT INTO settings(account, key, value) VALUES (%s, %s, %s) "
                    "ON CONFLICT(account, key) DO UPDATE SET value=excluded.value",
                    (acc, key, _json.dumps(value, ensure_ascii=False)),
                )
        conn.commit()
    finally:
        conn.close()


def record_health(source: str, ok: bool, detail: str = "", account: str | None = None) -> None:
    """Хартбит источника: время прогона + успех + причина (мониторинг надёжности).
    source — feature (apply/tests/reply/browse/giga/getmatch)."""
    import time as _t
    set_setting(f"_health.{source}",
                {"ts": int(_t.time()), "ok": bool(ok), "detail": (detail or "")[:200]},
                account=account)


def read_health(source: str, account: str | None = None) -> dict | None:
    v = get_setting(f"_health.{source}", None, account=account)
    return v if isinstance(v, dict) else None


def feature_enabled(feat: str, account: str | None = None) -> bool:
    """Тумблер функции из Mini App. Ключ settings `feat.<feat>`, по умолчанию ВКЛ."""
    return bool(get_setting(f"feat.{feat}", True, account=account))


def seen_keys(kind: str) -> set:
    acc = get_account()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key FROM seen_keys WHERE account=%s AND kind=%s", (acc, kind)
            )
            return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def add_seen(kind: str, keys) -> None:
    # keys может быть как списком, так и ОДНОЙ строкой. Без этого str итерировался бы
    # посимвольно (add_seen(kind, "5565") -> ключи '5','6'…), и vid не помечался seen.
    if isinstance(keys, (str, bytes, int)):
        keys = [keys]
    acc = get_account()
    conn = connect()
    try:
        with conn.cursor() as cur:
            for key in keys:
                cur.execute(
                    "INSERT INTO seen_keys(account, kind, key) VALUES (%s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (acc, kind, str(key)),
                )
        conn.commit()
    finally:
        conn.close()


def add_action_items(items: list[dict]) -> None:
    acc = get_account()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE action_items "
                        "ADD COLUMN IF NOT EXISTS action_url text")  # ссылка на анкету/тест
            for it in items:
                # дедуп: не плодим дело, если по этой вакансии (nid) уже есть невыполненное
                cur.execute("SELECT 1 FROM action_items WHERE account=%s AND nid=%s "
                            "AND NOT done LIMIT 1", (acc, it.get("nid")))
                if cur.fetchone():
                    continue
                cur.execute(
                    "INSERT INTO action_items(account, nid, chat_id, vacancy, action, "
                    "chat_url, vacancy_url, action_url) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (acc, it.get("nid"), it.get("chat_id"), it.get("vacancy"),
                     it.get("action"), it.get("chat_url"), it.get("vacancy_url"),
                     it.get("action_url")),
                )
        conn.commit()
    finally:
        conn.close()


def bump_activity(kind: str, n: int = 1, account: str | None = None) -> None:
    """Инкремент дневного счётчика активности (account, today, kind) += n.
    Зовётся из воркеров (HH_ACCOUNT в env задаёт run_all). Best-effort —
    сбой счётчика не должен ронять основной флоу (отправку отклика и т.п.)."""
    if n <= 0:
        return
    acc = account or get_account()
    try:
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO activity_daily(account, day, kind, count) "
                    "VALUES (%s, current_date, %s, %s) "
                    "ON CONFLICT(account, day, kind) "
                    "DO UPDATE SET count = activity_daily.count + excluded.count",
                    (acc, kind, n),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# --- OpenRouter роутинг (часть LLM-трафика гоним в OpenRouter, при исчерпании дневного
#     лимита — полностью на локалку). Конфиг в _global: or.token/or.model/or.endpoint/
#     or.share/or.daily_limit. Счётчик запросов за день — таблица or_usage. ---

def or_config() -> dict:
    """Конфиг OpenRouter из _global. Пустой dict, если не настроен (тогда 100% локалка)."""
    try:
        tok = get_setting("or.token", "", account="_global")
        if not tok:
            return {}
        return {
            "token": tok,
            "model": get_setting("or.model", "", account="_global"),
            "endpoint": get_setting("or.endpoint",
                                    "https://openrouter.ai/api/v1/chat/completions", account="_global"),
            "share": float(get_setting("or.share", "0.5", account="_global") or 0.5),
            "daily_limit": int(get_setting("or.daily_limit", "1000", account="_global") or 1000),
        }
    except Exception:
        return {}


def or_count_today() -> int:
    try:
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count FROM or_usage WHERE day = current_date")
                r = cur.fetchone()
                return int(r[0]) if r else 0
        finally:
            conn.close()
    except Exception:
        return 0


def or_bump() -> int:
    """Атомарно +1 к счётчику OpenRouter за сегодня, возвращает новое значение (0 при сбое БД)."""
    try:
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO or_usage(day, count) VALUES (current_date, 1) "
                    "ON CONFLICT(day) DO UPDATE SET count = or_usage.count + 1 RETURNING count")
                n = int(cur.fetchone()[0])
            conn.commit()
            return n
        finally:
            conn.close()
    except Exception:
        return 0


def or_exhaust() -> None:
    """Пометить дневной лимит OpenRouter исчерпанным (получили 429) — до конца суток на локалку."""
    try:
        lim = int(get_setting("or.daily_limit", "1000", account="_global") or 1000)
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO or_usage(day, count) VALUES (current_date, %s) "
                    "ON CONFLICT(day) DO UPDATE SET count = GREATEST(or_usage.count, %s)", (lim, lim))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# --- Уведомления ---
PRIORITY_HIGH = 1
PRIORITY_MED = 2
PRIORITY_LOW = 3


def notify(priority: int, text: str, category: str | None = None,
           link: str | None = None, dedup_key: str | None = None,
           account: str | None = None) -> None:
    acc = account or get_account()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notifications(account, priority, category, text, link, dedup_key) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (account, dedup_key) DO NOTHING",
                (acc, priority, category, text, link, dedup_key),
            )
        conn.commit()
    finally:
        conn.close()


# --- Телефон/шифрование/TG-api (без изменений) ---

def _norm_phone(p) -> str:
    d = "".join(ch for ch in str(p or "") if ch.isdigit())
    return d[-10:]


def _session_key() -> bytes:
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
        return s
