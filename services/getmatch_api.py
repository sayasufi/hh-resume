"""Клиент API GetMatch (getmatch.ru) — отклики и статусы напрямую через API.

Логин: `POST /api/auth/otp {username, role:"candidate"}` → код приходит в @g_jobbot →
читаем Telethon-сессией → `POST /api/auth/authorize {login_username, code, role}` →
кука `AIOHTTP_SESSION` (храним в setting `getmatch.session`, переиспользуем; релогин при 401).
`username` берём из самой Telegram-сессии аккаунта (get_me().username).

Отклик:  `GET /api/offers?exclude_applied=true&...` → `POST /api/offers/{id}/apply` (multipart:
first_name,last_name,salary_currency,salary_from,location_id,web_apply_source) → {application_id}.
Статусы: `GET /api/applications/candidate?section=all` → status/status_readable/applied_at/reject_reason.
"""
import asyncio
import re

import httpx

from hh_applicant_tool.storage import pgconn

BASE = "https://getmatch.ru"
BOT = "g_jobbot"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
_CODE_RE = re.compile(r"\b(\d{4,6})\b")
ROLE = "candidate"


class GetMatchError(RuntimeError):
    pass


# ── чистые хелперы (тестируемые) ─────────────────────────────────────────────
def abs_url(u: str) -> str:
    """Относительную ссылку getmatch → абсолютную."""
    u = u or ""
    return BASE + u if u.startswith("/") else u


def apply_form(offer: dict, me: dict) -> dict:
    """Поля multipart-отклика из профиля кандидата + локации вакансии."""
    locs = offer.get("location_requirements") or []
    location_id = (locs[0].get("location_id") if locs else "") or ""
    return {
        "first_name": me.get("first_name") or "",
        "last_name": me.get("last_name") or "",
        "salary_currency": me.get("salary_currency") or "RUB",
        "salary_from": str(me.get("salary_from") or 0),
        "location_id": location_id,
        "web_apply_source": "offers_list",
    }


def profile_filters(me: dict) -> dict:
    """Фильтры ленты из профиля кандидата (специализации/локации/зарплата) — чтобы
    откликаться только на подходящее, как в курированном пуше бота."""
    f = {}
    if me.get("specializations"):
        f["sp"] = me["specializations"]
    if me.get("locations"):
        f["l"] = me["locations"]
    if me.get("salary_from"):
        f["sa"] = me["salary_from"]
    return f


class GetMatchAPI:
    """Тонкий клиент API GetMatch для одного аккаунта."""

    def __init__(self, account: str, tg_session_enc: str):
        self.account = account
        self.tg_session_enc = tg_session_enc
        self.username = None  # Telegram username (логин), берём из сессии при логине
        self.hc = httpx.AsyncClient(
            base_url=BASE, timeout=30.0, follow_redirects=True,
            headers={"User-Agent": UA, "Origin": BASE, "Referer": BASE + "/applications"})

    async def aclose(self):
        await self.hc.aclose()

    # ── Telethon: имя пользователя + чтение OTP-кода ──────────────────────────
    def _tg_client(self):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        api_id, api_hash = pgconn.tg_api()
        return TelegramClient(StringSession(pgconn.dec_session(self.tg_session_enc)), api_id, api_hash)

    async def _otp_login(self):
        """OTP-логин: запросить код, прочитать из бота, авторизоваться, сохранить сессию."""
        cl = self._tg_client()
        await cl.connect()
        try:
            me_tg = await cl.get_me()
            self.username = (me_tg.username or "").lstrip("@")
            if not self.username:
                raise GetMatchError("у Telegram-аккаунта нет username — логин GetMatch невозможен")
            ent = await cl.get_entity(BOT)
            before = (await cl.get_messages(ent, limit=1))[0].id
            r = await self.hc.post("/api/auth/otp", json={
                "username": self.username, "role": ROLE, "register_if_not_found": False})
            if r.status_code != 200:
                raise GetMatchError(f"otp {r.status_code}: {r.text[:120]}")
            code = None
            for _ in range(10):
                await asyncio.sleep(3)
                for m in await cl.get_messages(ent, limit=6):
                    if m.id > before and not m.out and "код" in (m.text or "").lower():
                        mm = _CODE_RE.search(m.text or "")
                        if mm:
                            code = mm.group(1); break
                if code:
                    break
            if not code:
                raise GetMatchError("OTP-код не пришёл в бот")
        finally:
            await cl.disconnect()
        r2 = await self.hc.post("/api/auth/authorize", json={
            "login_username": self.username, "code": code, "role": ROLE})
        if r2.status_code != 200:
            raise GetMatchError(f"authorize {r2.status_code}: {r2.text[:120]}")
        sess = self.hc.cookies.get("AIOHTTP_SESSION")
        if sess:
            pgconn.set_setting("getmatch.session", sess, account=self.account)
        return True

    async def ensure_auth(self):
        """Переиспользовать сохранённую сессию; при невалидности — OTP-релогин. Возвращает /me."""
        sess = pgconn.get_setting("getmatch.session", account=self.account)
        if sess:
            self.hc.cookies.set("AIOHTTP_SESSION", sess, domain="getmatch.ru")
            r = await self.hc.get("/api/auth/me")
            if r.status_code == 200:
                return r.json()
        await self._otp_login()
        r = await self.hc.get("/api/auth/me")
        if r.status_code != 200:
            raise GetMatchError(f"после логина /me = {r.status_code}")
        return r.json()

    # ── данные ────────────────────────────────────────────────────────────────
    async def offers(self, limit: int = 50, **filters) -> list:
        """Подходящие вакансии (исключая уже-откликнутые)."""
        params = {"exclude_applied": "true", "offset": 0, "limit": limit, "pa": "all"}
        params.update(filters)
        r = await self.hc.get("/api/offers", params=params)
        r.raise_for_status()
        return r.json().get("offers", [])

    async def apply(self, offer: dict, me: dict) -> httpx.Response:
        """Откликнуться на вакансию (multipart, данные из профиля + локация вакансии)."""
        form = {k: (None, v) for k, v in apply_form(offer, me).items()}
        return await self.hc.post(f"/api/offers/{offer['id']}/apply", files=form)

    async def applications(self, limit: int = 100) -> list:
        """Все наши отклики со статусами."""
        r = await self.hc.get("/api/applications/candidate",
                               params={"section": "all", "offset": 0, "limit": limit})
        r.raise_for_status()
        return r.json().get("applications", [])
