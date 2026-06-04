"""Mini App бэкенд (FastAPI): профиль + статистика + тумблеры функций.

Запуск: uvicorn web_app:app --host 127.0.0.1 --port 60080  (через run_webapp.sh).
Авторизация: Telegram WebApp initData (HMAC-SHA256 токеном бота) -> tg_user_id ->
hh-аккаунт (по app_config.telegram.user_id, который пишет /connect). Всё строго
по найденному account. Наружу только через nginx+TLS (uvicorn слушает 127.0.0.1).
"""
import asyncio
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp_static")
FEATURES = ("apply", "tests", "reply", "browse", "notify", "giga")  # тумблеры
MAX_PER_DAY_CAP = 200   # серверный суточный потолок откликов hh (защита от бана)
TESTS_PER_DAY_CAP = 30  # практический потолок браузерного тест-флоу
_INITDATA_MAX_AGE = 86400  # сутки

app = FastAPI(title="hh Mini App")


@app.middleware("http")
async def _no_cache(request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.endswith((".html", ".css", ".js")):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


def _ensure_tables() -> None:
    try:
        conn = pgconn.connect()
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS stats_daily ("
                "account text NOT NULL, day date NOT NULL, applications int DEFAULT 0, "
                "views int DEFAULT 0, invitations int DEFAULT 0, "
                "PRIMARY KEY (account, day))"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS dlg_cache ("
                "account text NOT NULL, nid text NOT NULL, title text, employer text, "
                "state_id text, state text, emoji text, rank int DEFAULT 2, "
                "has_updates boolean DEFAULT false, url text, updated text, "
                "ts timestamptz DEFAULT now(), PRIMARY KEY (account, nid))"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS activity_daily ("
                "account text NOT NULL, day date NOT NULL, kind text NOT NULL, "
                "count int NOT NULL DEFAULT 0, PRIMARY KEY (account, day, kind))"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS giga_queue ("
                "account text NOT NULL, token text NOT NULL, vacancy text, nid bigint, "
                "status text NOT NULL DEFAULT 'pending', turns int NOT NULL DEFAULT 0, "
                "created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(), "
                "PRIMARY KEY (account, token))"
            )
            # действия-«дела»: колонка done могла отсутствовать в старой таблице
            cur.execute("ALTER TABLE action_items "
                        "ADD COLUMN IF NOT EXISTS done boolean NOT NULL DEFAULT false")
        conn.commit()
        conn.close()
    except Exception as e:
        print("stats_daily ensure:", repr(e)[:120])


_ensure_tables()

# ── авторизация (Telegram initData) ─────────────────────────────────────────

_bot_token_cache = {"v": None, "t": 0.0}


def _bot_token() -> str | None:
    if _bot_token_cache["v"] and time.time() - _bot_token_cache["t"] < 300:
        return _bot_token_cache["v"]
    for _name, acc in pgconn.list_users():
        tg = pgconn.app_config(account=acc).get("telegram") or {}
        if tg.get("token"):
            _bot_token_cache["v"] = tg["token"]
            _bot_token_cache["t"] = time.time()
            return tg["token"]
    return None


def _validate_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(401, "no init data")
    token = _bot_token()
    if not token:
        raise HTTPException(503, "bot token unavailable")
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    recv_hash = parsed.pop("hash", "")
    if not recv_hash:
        raise HTTPException(401, "no hash")
    check = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv_hash):
        raise HTTPException(401, "bad signature")
    try:
        if time.time() - int(parsed.get("auth_date", "0")) > _INITDATA_MAX_AGE:
            raise HTTPException(401, "init data expired")
    except ValueError:
        raise HTTPException(401, "bad auth_date")
    try:
        return json.loads(parsed.get("user", "{}"))
    except (ValueError, TypeError):
        raise HTTPException(401, "bad user")


def _account_for_user(tg_user_id) -> str | None:
    """Маппинг по app_config.tg_user_id (пишет /connect; та же схема, что _account_by)."""
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT account, value FROM app_config WHERE key='tg_user_id'")
            for acc, val in cur.fetchall():
                if str(val) == str(tg_user_id):
                    return acc
    finally:
        conn.close()
    return None


# Админ: может смотреть/настраивать ЛЮБОЙ аккаунт. По tg_user_id (стабильно) + username.
ADMIN_TG_IDS = {"5222335152"}
ADMIN_USERNAMES = {"throlib"}


def _is_admin(user: dict) -> bool:
    return (str(user.get("id")) in ADMIN_TG_IDS
            or (user.get("username") or "").lower() in ADMIN_USERNAMES)


def _all_accounts() -> list:
    return [{"account": a, "name": n} for n, a in pgconn.list_users()]


async def _auth(init_data: str, account: str | None = None) -> str:
    """Эффективный аккаунт. Для админа `account`-override разрешён (любой аккаунт)."""
    user = await asyncio.to_thread(_validate_init_data, init_data)
    if account and _is_admin(user):
        return account
    acc = await asyncio.to_thread(_account_for_user, user.get("id"))
    if not acc:
        raise HTTPException(404, "not_linked")
    return acc


# ── статистика ──────────────────────────────────────────────────────────────

_me_cache: dict = {}  # account -> (ts, payload)


async def _hh_stats(account: str) -> dict:
    """Живые цифры из hh API (best-effort: ошибки -> нули)."""
    cfg = await asyncio.to_thread(pgconn.app_config, account)
    token = cfg.get("token") or {}
    out = {"applications_total": 0, "resume_views": 0, "invitations": 0,
           "responses": 0, "resume_title": "", "hh_id": None, "full_name": ""}
    if not token.get("access_token"):
        return out
    api = ApiClient(
        access_token=token["access_token"],
        refresh_token=token.get("refresh_token", ""),
        access_expires_at=token.get("access_expires_at", 0),
        user_agent=generate_android_useragent(),
    )
    try:
        try:
            me = await api.get("/me")
            out["hh_id"] = me.get("id")
            out["full_name"] = " ".join(
                x for x in [me.get("last_name"), me.get("first_name")] if x
            )
        except Exception:
            pass
        try:
            resumes = (await api.get("/resumes/mine")).get("items", [])
            pub = [r for r in resumes
                   if (r.get("status") or {}).get("id") == "published"] or resumes
            if pub:
                out["resume_title"] = pub[0].get("title", "")
            for r in resumes:
                c = r.get("counters") or {}
                out["resume_views"] += int(c.get("total_views") or 0)
                out["invitations"] += int(c.get("invitations") or 0)
        except Exception:
            pass
        try:
            neg = await api.get("/negotiations", per_page=1)
            out["applications_total"] = int(neg.get("found") or 0)
        except Exception:
            pass
    finally:
        await api.aclose()
    out["responses"] = max(out["applications_total"] - out["invitations"], 0)
    return out


def _db_stats(account: str) -> dict:
    """Цифры из БД: отклики сегодня + приглашения на интервью.
    Интервью считаем СТРОГО по category='interview' (ставит reply-employers при
    реальном приглашении), а НЕ по всем HIGH-уведомлениям — туда же падают алерты
    мониторинга/сбои/contact-дела, которые раздували «Интервью»."""
    today = int(pgconn.get_setting("_applications_count", 0, account=account) or 0)
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM notifications "
                "WHERE account=%s AND category='interview'",
                (account,),
            )
            interviews = cur.fetchone()[0]
    finally:
        conn.close()
    return {"applications_today": today, "interviews": interviews}


async def _hh_call(account: str, path: str, **params):
    """Один GET к hh API под токеном аккаунта. None при ошибке."""
    cfg = await asyncio.to_thread(pgconn.app_config, account)
    token = cfg.get("token") or {}
    if not token.get("access_token"):
        return None
    api = ApiClient(
        access_token=token["access_token"],
        refresh_token=token.get("refresh_token", ""),
        access_expires_at=token.get("access_expires_at", 0),
        user_agent=generate_android_useragent(),
    )
    try:
        return await api.get(path, **params)
    except Exception:
        return None
    finally:
        await api.aclose()


async def _resume_list(account: str) -> list:
    data = await _hh_call(account, "/resumes/mine")
    items = (data or {}).get("items", [])
    return [{"id": r.get("id"), "title": r.get("title", "")} for r in items]


_NEG_STATE = {
    "hired": ("🎉", "Оффер"),
    "interview": ("🤝", "Собеседование"), "invitation": ("🤝", "Собеседование"),
    "response": ("💬", "Ответ"), "discard": ("🔴", "Отказ"),
    "discard_by_applicant": ("⚪️", "Отозван"), "hidden": ("⚪️", "Скрыт"),
}
_STATE_RANK = {"hired": 0, "interview": 1, "invitation": 1, "response": 2,
               "discard": 4, "hidden": 5}


async def _sync_dialogs(account: str) -> int:
    """Тянем ВСЕ отклики из hh (постранично) и кладём в dlg_cache. -> кол-во."""
    rows, page = [], 0
    while page < 8:  # до 800 откликов
        data = await _hh_call(account, "/negotiations", per_page=100, page=page,
                              order_by="updated_at")
        if not data:
            break
        for n in data.get("items", []):
            vac = n.get("vacancy") or {}
            sid = (n.get("state") or {}).get("id") or ""
            emoji, label = _NEG_STATE.get(
                sid, ("•", (n.get("state") or {}).get("name") or sid))
            rows.append((
                account, str(n.get("id")), vac.get("name") or "Вакансия",
                (vac.get("employer") or {}).get("name") or "", sid, label, emoji,
                _STATE_RANK.get(sid, 2), bool(n.get("has_updates")),
                vac.get("alternate_url") or "", (n.get("updated_at") or "")[:10],
            ))
        if page + 1 >= (data.get("pages") or 1):
            break
        page += 1
    if not rows:
        return 0

    def _write():
        conn = pgconn.connect()
        try:
            with conn.cursor() as cur:
                for r in rows:
                    cur.execute(
                        "INSERT INTO dlg_cache(account, nid, title, employer, state_id, "
                        "state, emoji, rank, has_updates, url, updated, ts) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) "
                        "ON CONFLICT(account, nid) DO UPDATE SET title=excluded.title, "
                        "employer=excluded.employer, state_id=excluded.state_id, "
                        "state=excluded.state, emoji=excluded.emoji, rank=excluded.rank, "
                        "has_updates=excluded.has_updates, url=excluded.url, "
                        "updated=excluded.updated, ts=now()", r)
            conn.commit()
        finally:
            conn.close()

    await asyncio.to_thread(_write)
    return len(rows)


def _range_sql(col: str, dfrom, dto):
    """-> (доп. условие SQL, параметры) для фильтра по диапазону дат [dfrom..dto].
    col — имя колонки даты ('updated' text YYYY-MM-DD или 'day' date). Пусто = без границы."""
    cond, params = "", []
    if dfrom:
        cond += f" AND {col} >= %s"
        params.append(dfrom)
    if dto:
        cond += f" AND {col} <= %s"
        params.append(dto)
    return cond, params


def _dlg_meta(account: str):
    """(всего строк в кэше, возраст последнего синка в сек) — для решения о синке."""
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), extract(epoch FROM now()-max(ts)) "
                        "FROM dlg_cache WHERE account=%s", (account,))
            cnt, age = cur.fetchone()
        return int(cnt or 0), (age if age is not None else 1e9)
    finally:
        conn.close()


def _dlg_read(account: str, limit: int, dfrom=None, dto=None):
    cond, params = _range_sql("updated", dfrom, dto)
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM dlg_cache WHERE account=%s" + cond,
                        [account] + params)
            cnt = cur.fetchone()[0]
            cur.execute(
                "SELECT nid, title, employer, state_id, state, emoji, rank, "
                "has_updates, url, updated FROM dlg_cache WHERE account=%s" + cond
                + " ORDER BY updated DESC NULLS LAST LIMIT %s",
                [account] + params + [limit])
            items = []
            for r in cur.fetchall():
                sid = r[3] or ""
                emoji, label = _NEG_STATE.get(sid, (r[5] or "•", r[4] or sid))
                items.append({
                    "id": r[0], "title": r[1], "employer": r[2], "state_id": sid,
                    "state": label, "emoji": emoji,
                    "rank": _STATE_RANK.get(sid, r[6] if r[6] is not None else 3),
                    "has_updates": r[7], "url": r[8], "updated": r[9],
                })
        return items, int(cnt or 0)
    finally:
        conn.close()


def _state_counts(account: str, dfrom=None, dto=None) -> dict:
    cond, params = _range_sql("updated", dfrom, dto)
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT state_id, count(*) FROM dlg_cache WHERE account=%s"
                        + cond + " GROUP BY state_id", [account] + params)
            return {k: v for k, v in cur.fetchall()}
    finally:
        conn.close()


def _funnel_from_states(c: dict) -> list:
    """Воронка: Отклики → Ответили → Собеседования (invitation легаси = interview,
    hired считаем в собеседованиях). Без отдельного этапа «Оффер»."""
    sob = c.get("interview", 0) + c.get("invitation", 0) + c.get("hired", 0)
    resp = c.get("response", 0)
    stages = [("Отклики", sum(c.values())), ("Ответили", resp + sob),
              ("Собеседования", sob)]
    out, prev = [], None
    for label, val in stages:
        out.append({"label": label, "value": val,
                    "conv": round(val / prev * 100) if prev else None})
        prev = val
    return out


def _breakdown(c: dict) -> list:
    """Детальная разбивка по статусам (с % от всех откликов)."""
    total = sum(c.values()) or 1
    sob = c.get("interview", 0) + c.get("invitation", 0) + c.get("hired", 0)
    rows = [
        ("🤝", "Собеседования", sob),
        ("💬", "Ответ / на рассмотрении", c.get("response", 0)),
        ("🔴", "Отказы", c.get("discard", 0) + c.get("discard_by_applicant", 0)),
    ]
    other = c.get("hidden", 0)
    if other:
        rows.append(("⚪️", "Прочее", other))
    return [{"emoji": e, "label": lbl, "value": v, "pct": round(v / total * 100)}
            for e, lbl, v in rows]


def _kpis(c: dict) -> dict:
    """Ключевые конверсии воронки в абсолютных % (данные уже в counts)."""
    total = sum(c.values())
    if not total:
        return {}
    sob = c.get("interview", 0) + c.get("invitation", 0) + c.get("hired", 0)
    answered = c.get("response", 0) + sob  # ответили = ответ + дошли дальше
    return {
        "total": total,
        "response_rate": round(answered / total * 100),  # отклик → ответ
        "interview_rate": round(sob / total * 100),       # отклик → собеседование
        "offer_rate": round(c.get("hired", 0) / total * 100),  # отклик → оффер
    }


def _giga_summary(account: str) -> dict:
    """Сводка авто-ГигаРекрутера из giga_queue (прогресс интервью)."""
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, count(*) FROM giga_queue WHERE account=%s "
                        "GROUP BY status", (account,))
            by = {k: int(v) for k, v in cur.fetchall()}
            cur.execute("SELECT vacancy, updated_at FROM giga_queue WHERE account=%s "
                        "AND status='done' ORDER BY updated_at DESC LIMIT 1", (account,))
            row = cur.fetchone()
    finally:
        conn.close()
    last = {"vacancy": row[0] or "", "at": str(row[1])[:16]} if row else None
    return {"pending": by.get("pending", 0), "done": by.get("done", 0),
            "active": by.get("active", 0) + by.get("running", 0), "last": last}


async def _dialog_messages(account: str, nid: str) -> dict:
    data = await _hh_call(account, f"/negotiations/{nid}/messages")
    msgs = []
    for m in (data or {}).get("items", []):
        author = ((m.get("author") or {}).get("participant_type") or "")
        msgs.append({
            "me": author == "applicant",
            "text": (m.get("text") or "").strip(),
            "at": (m.get("created_at") or "")[:16].replace("T", " "),
        })
    return {"messages": msgs}


def _snapshot(account: str, apps: int, views: int, invitations: int) -> None:
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO stats_daily(account, day, applications, views, invitations) "
                "VALUES (%s, current_date, %s, %s, %s) "
                "ON CONFLICT(account, day) DO UPDATE SET applications=excluded.applications, "
                "views=excluded.views, invitations=excluded.invitations",
                (account, apps, views, invitations),
            )
        conn.commit()
    finally:
        conn.close()


def _trends(account: str) -> list:
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT day, applications, views, invitations FROM stats_daily "
                "WHERE account=%s AND day >= current_date - 29 ORDER BY day",
                (account,),
            )
            return [{"day": str(d), "applications": a, "views": v, "invitations": i}
                    for d, a, v, i in cur.fetchall()]
    finally:
        conn.close()


def _activity(account: str, dfrom=None, dto=None) -> dict:
    """Сумма count по kind из activity_daily за диапазон [dfrom..dto]."""
    cond, params = _range_sql("day", dfrom, dto)
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT kind, COALESCE(SUM(count),0) FROM activity_daily "
                        "WHERE account=%s" + cond + " GROUP BY kind", [account] + params)
            agg = {k: int(v) for k, v in cur.fetchall()}
    finally:
        conn.close()
    return {k: agg.get(k, 0) for k in ("apply", "tests", "reply", "browse", "bump")}


def _action_items(account: str) -> list:
    """Актуальные «дела» (не выполненные, за 30 дней), новые сверху."""
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, vacancy, action, chat_url, created_at FROM action_items "
                "WHERE account=%s AND NOT done "
                "AND created_at > now() - interval '30 days' "
                "ORDER BY created_at DESC LIMIT 100", (account,))
            return [{"id": r[0], "vacancy": r[1] or "", "action": r[2] or "",
                     "chat_url": r[3] or "", "created_at": (str(r[4])[:16] if r[4] else "")}
                    for r in cur.fetchall()]
    finally:
        conn.close()


def _action_done(account: str, aid: int) -> None:
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE action_items SET done=true WHERE account=%s AND id=%s",
                        (account, aid))
        conn.commit()
    finally:
        conn.close()


def _funnel(apps: int, invitations: int, interviews: int) -> list:
    """Последовательная воронка: каждый этап ⊆ предыдущего + конверсия %."""
    stages = [("Отклики", apps), ("Приглашения", invitations),
              ("Интервью", min(interviews, invitations) if invitations else interviews)]
    out = []
    prev = None
    for label, val in stages:
        conv = round(val / prev * 100) if prev else None
        out.append({"label": label, "value": val, "conv": conv})
        prev = val
    return out


def _next_apply(apply_on: bool, pause_until: str, today: int, limit: int):
    """Следующий запуск обычных откликов. apply-similar по расписанию ежечасно в
    окне 08–22 МСК (Prefect, cron `0 5-19 * * *` UTC, +джиттер ~5 мин), НО встаёт
    на паузу до завтра при дневном лимите (`_applications_pause_until`) или
    серверном LimitExceeded от hh. Окно 5–19 UTC — единственный источник часов здесь."""
    if not apply_on:
        return None
    now = datetime.utcnow()
    now_msk = now + timedelta(hours=3)
    utc_today = now.date().isoformat()
    paused = (pause_until and pause_until > utc_today) or (limit and today >= limit)
    if paused:  # дневной лимит достигнут -> возобновится завтра в начале окна
        extra = f" ({today}/{limit})" if (limit and today) else ""
        return f"завтра ~08:00 МСК · дневной лимит на сегодня достигнут{extra}"
    nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(48):
        if 5 <= nxt.hour <= 19:
            break
        nxt += timedelta(hours=1)
    msk = nxt + timedelta(hours=3)
    tomorrow = msk.date() != now_msk.date()
    mins = max(0, int((nxt - now).total_seconds() // 60))
    label = ("завтра " if tomorrow else "") + "~" + msk.strftime("%H:%M") + " МСК"
    if not tomorrow and mins <= 90:
        label += f" (через {mins} мин)"
    if limit:
        label += f" · сегодня {today}/{limit}"
    return label


async def _build_me(account: str, dfrom=None, dto=None) -> dict:
    key = (account, dfrom, dto)
    cached = _me_cache.get(key)
    if cached and time.time() - cached[0] < 60:
        return cached[1]
    hh, db = await _hh_stats(account), await asyncio.to_thread(_db_stats, account)
    if hh["hh_id"]:  # токен жив -> фиксируем дневной срез для трендов
        await asyncio.to_thread(_snapshot, account, hh["applications_total"],
                                hh["resume_views"], hh["invitations"])
    counts = await asyncio.to_thread(_state_counts, account, dfrom, dto)  # за период
    cfg = await asyncio.to_thread(pgconn.app_config, account)
    name = (await asyncio.to_thread(
        pgconn.get_setting, "user.full_name", None, account)) or hh["full_name"] or account
    salary = (cfg.get("preferences") or {}).get("salary") or ""
    flags = [await asyncio.to_thread(pgconn.feature_enabled, f, account)
             for f in FEATURES]
    on = sum(1 for f in flags if f)
    status_kind = "ok" if on == len(flags) else "off" if on == 0 else "paused"
    status = ("работает" if status_kind == "ok"
              else "всё на паузе" if status_kind == "off"
              else "часть функций на паузе")
    has_cache = bool(sum(counts.values()))
    funnel = _funnel_from_states(counts) if has_cache else _funnel(
        hh["applications_total"], hh["invitations"], db["interviews"])
    payload = {
        "profile": {
            "name": name, "hh_id": hh["hh_id"], "resume": hh["resume_title"],
            "salary": salary, "status": status, "status_kind": status_kind,
        },
        "stats": {
            "funnel": funnel,
            "breakdown": _breakdown(counts) if has_cache else [],
            "kpis": _kpis(counts) if has_cache else {},
        },
        "next_apply": _next_apply(
            flags[0],
            await asyncio.to_thread(
                pgconn.get_setting, "_applications_pause_until", "", account) or "",
            db["applications_today"],
            int(await asyncio.to_thread(
                pgconn.get_setting, "apply.max_per_day", 15, account) or 0)),
    }
    _me_cache[key] = (time.time(), payload)
    return payload


# ── API ─────────────────────────────────────────────────────────────────────

@app.get("/api/me")
async def api_me(dfrom: str = None, dto: str = None, account: str = None,
                 x_init_data: str = Header(None, alias="X-Init-Data")):
    user = await asyncio.to_thread(_validate_init_data, x_init_data)
    admin = _is_admin(user)
    acc = account if (account and admin) else await asyncio.to_thread(
        _account_for_user, user.get("id"))
    if not acc:
        raise HTTPException(404, "not_linked")
    data = await _build_me(acc, dfrom, dto)
    data["is_admin"] = admin
    data["account"] = acc
    if admin:
        data["accounts"] = await asyncio.to_thread(_all_accounts)
    return data


@app.get("/api/settings")
async def api_settings(account: str = None,
                       x_init_data: str = Header(None, alias="X-Init-Data")):
    account = await _auth(x_init_data, account)
    cfg = await asyncio.to_thread(pgconn.app_config, account)
    features = {f: await asyncio.to_thread(pgconn.feature_enabled, f, account)
                for f in FEATURES}
    config = {
        "salary": (cfg.get("preferences") or {}).get("salary") or "",
        "max_per_day": await asyncio.to_thread(
            pgconn.get_setting, "apply.max_per_day", 15, account),
        "tests_per_day": await asyncio.to_thread(
            pgconn.get_setting, "apply.tests_per_day", 10, account),
        "resume_id": await asyncio.to_thread(
            pgconn.get_setting, "apply.resume_id", "", account),
        "civil_law_only": bool(await asyncio.to_thread(
            pgconn.get_setting, "apply.civil_law_only", False, account)),
        "max_per_day_cap": MAX_PER_DAY_CAP,   # серверный суточный потолок hh
        "tests_per_day_cap": TESTS_PER_DAY_CAP,  # практический потолок тест-флоу
    }
    return {"features": features, "config": config,
            "resumes": await _resume_list(account),
            # подключён ли Telegram-юзербот (нужен для авто-ГигаРекрутера)
            "tg_connected": bool(cfg.get("tg_user_session"))}


async def _set_config(account: str, key: str, value) -> None:
    if key in FEATURES:
        # ГигаРекрутер нельзя включить без подключённого Telegram (user-сессии):
        # бот действует от лица пользователя в чате @Giga_recruiter_bot.
        if key == "giga" and bool(value):
            cfg = await asyncio.to_thread(pgconn.app_config, account)
            if not cfg.get("tg_user_session"):
                raise HTTPException(
                    400, "Подключите Telegram (кнопка «Подключить» / команда /connect "
                         "в боте), чтобы включить ГигаРекрутера.")
        await asyncio.to_thread(pgconn.set_setting, f"feat.{key}", bool(value), account)
    elif key == "salary":
        cfg = await asyncio.to_thread(pgconn.app_config, account)
        prefs = cfg.get("preferences") or {}
        prefs["salary"] = str(value).strip()
        await asyncio.to_thread(pgconn.set_app_config, "preferences", prefs, account)
    elif key in ("apply.max_per_day", "apply.tests_per_day"):
        cap = MAX_PER_DAY_CAP if key == "apply.max_per_day" else TESTS_PER_DAY_CAP
        await asyncio.to_thread(
            pgconn.set_setting, key, min(cap, max(0, int(value))), account)
    elif key == "apply.resume_id":
        await asyncio.to_thread(pgconn.set_setting, "apply.resume_id", str(value), account)
    elif key == "apply.civil_law_only":
        await asyncio.to_thread(pgconn.set_setting, key, bool(value), account)
    else:
        raise HTTPException(400, "unknown key")


@app.post("/api/settings")
async def api_settings_set(body: dict, account: str = None,
                           x_init_data: str = Header(None, alias="X-Init-Data")):
    account = await _auth(x_init_data, account)
    key = body.get("key")
    try:
        await _set_config(account, key, body.get("value"))
    except (ValueError, TypeError):
        raise HTTPException(400, "bad value")
    for k in [k for k in _me_cache if k[0] == account]:  # ключ — кортеж (acc,dfrom,dto)
        _me_cache.pop(k, None)
    return {"ok": True, "key": key}


@app.get("/api/dialogs")
async def api_dialogs(dfrom: str = None, dto: str = None, limit: int = 500,
                      account: str = None,
                      x_init_data: str = Header(None, alias="X-Init-Data")):
    account = await _auth(x_init_data, account)
    total, age = await asyncio.to_thread(_dlg_meta, account)
    if total == 0:                     # пусто -> синхронно тянем первый раз
        await _sync_dialogs(account)
    elif age > 900:                    # старше 15 мин -> освежаем в фоне
        asyncio.create_task(_sync_dialogs(account))
    items, cnt = await asyncio.to_thread(_dlg_read, account, limit, dfrom, dto)
    return {"items": items, "total": cnt}


@app.get("/api/dialog")
async def api_dialog(id: str, account: str = None,
                     x_init_data: str = Header(None, alias="X-Init-Data")):
    account = await _auth(x_init_data, account)
    return await _dialog_messages(account, id)


@app.get("/api/activity")
async def api_activity(dfrom: str = None, dto: str = None, account: str = None,
                       x_init_data: str = Header(None, alias="X-Init-Data")):
    account = await _auth(x_init_data, account)
    return await asyncio.to_thread(_activity, account, dfrom, dto)


@app.get("/api/actions")
async def api_actions(account: str = None,
                      x_init_data: str = Header(None, alias="X-Init-Data")):
    account = await _auth(x_init_data, account)
    return {"items": await asyncio.to_thread(_action_items, account)}


@app.post("/api/action_done")
async def api_action_done(body: dict, account: str = None,
                          x_init_data: str = Header(None, alias="X-Init-Data")):
    account = await _auth(x_init_data, account)
    try:
        aid = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "bad id")
    await asyncio.to_thread(_action_done, account, aid)
    return {"ok": True}


@app.get("/api/trends")
async def api_trends(account: str = None,
                     x_init_data: str = Header(None, alias="X-Init-Data")):
    account = await _auth(x_init_data, account)
    return {"days": await asyncio.to_thread(_trends, account)}


@app.get("/api/giga")
async def api_giga(account: str = None,
                   x_init_data: str = Header(None, alias="X-Init-Data")):
    account = await _auth(x_init_data, account)
    return await asyncio.to_thread(_giga_summary, account)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
