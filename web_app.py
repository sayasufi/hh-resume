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
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp_static")
FEATURES = ("apply", "reply", "browse", "giga")  # тумблеры
_INITDATA_MAX_AGE = 86400  # сутки

app = FastAPI(title="hh Mini App")

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


async def _auth(init_data: str) -> str:
    user = await asyncio.to_thread(_validate_init_data, init_data)
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
    """Цифры из БД: отклики сегодня + интервью/хендофф (notifications HIGH)."""
    today = int(pgconn.get_setting("_applications_count", 0, account=account) or 0)
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM notifications WHERE account=%s AND priority=%s",
                (account, pgconn.PRIORITY_HIGH),
            )
            interviews = cur.fetchone()[0]
    finally:
        conn.close()
    return {"applications_today": today, "interviews": interviews}


async def _build_me(account: str) -> dict:
    cached = _me_cache.get(account)
    if cached and time.time() - cached[0] < 60:
        return cached[1]
    hh, db = await _hh_stats(account), await asyncio.to_thread(_db_stats, account)
    cfg = await asyncio.to_thread(pgconn.app_config, account)
    name = (await asyncio.to_thread(
        pgconn.get_setting, "user.full_name", None, account)) or hh["full_name"] or account
    salary = (cfg.get("preferences") or {}).get("salary") or ""
    flags = [await asyncio.to_thread(pgconn.feature_enabled, f, account)
             for f in FEATURES]
    active = all(flags)
    payload = {
        "profile": {
            "name": name,
            "hh_id": hh["hh_id"],
            "resume": hh["resume_title"],
            "salary": salary,
            "status": "работает" if active else "часть функций на паузе",
        },
        "stats": {
            "applications_total": hh["applications_total"],
            "applications_today": db["applications_today"],
            "resume_views": hh["resume_views"],
            "responses": hh["responses"],
            "invitations": hh["invitations"],
            "interviews": db["interviews"],
            "funnel": [
                {"label": "Отклики", "value": hh["applications_total"]},
                {"label": "Просмотры", "value": hh["resume_views"]},
                {"label": "Приглашения", "value": hh["invitations"]},
                {"label": "Интервью", "value": db["interviews"]},
            ],
        },
    }
    _me_cache[account] = (time.time(), payload)
    return payload


# ── API ─────────────────────────────────────────────────────────────────────

@app.get("/api/me")
async def api_me(x_init_data: str = Header(None, alias="X-Init-Data")):
    account = await _auth(x_init_data)
    return await _build_me(account)


@app.get("/api/settings")
async def api_settings(x_init_data: str = Header(None, alias="X-Init-Data")):
    account = await _auth(x_init_data)
    return {f: await asyncio.to_thread(pgconn.feature_enabled, f, account)
            for f in FEATURES}


@app.post("/api/settings")
async def api_settings_set(body: dict,
                           x_init_data: str = Header(None, alias="X-Init-Data")):
    account = await _auth(x_init_data)
    key = body.get("key")
    if key not in FEATURES:
        raise HTTPException(400, "unknown feature")
    val = bool(body.get("value"))
    await asyncio.to_thread(pgconn.set_setting, f"feat.{key}", val, account)
    _me_cache.pop(account, None)
    return {"ok": True, "key": key, "value": val}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
