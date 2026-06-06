"""Habr Career source — внутренний frontend-API через httpx (offers/apply/statuses).
Браузер нужен ТОЛЬКО для логина (Playwright + 2captcha решает Yandex SmartCaptcha).
Сессия (cookies) хранится в settings `habr.session`, переиспользуется; релогин — когда
сессия протухла. Креды: `habr.login` / `habr.password`(enc) + общий `habr.2captcha_key`.
"""
import asyncio
import json
import re
import time
import urllib.parse
import urllib.request

import httpx

from hh_applicant_tool.storage import pgconn

BASE = "https://career.habr.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class HabrError(Exception):
    pass


def _2captcha(key, sitekey, pageurl):
    """Решить Yandex SmartCaptcha через 2captcha (legacy in.php). -> token."""
    q = urllib.parse.urlencode({"key": key, "method": "yandex", "sitekey": sitekey,
                                "pageurl": pageurl, "json": 1})
    sub = json.loads(urllib.request.urlopen(f"https://2captcha.com/in.php?{q}", timeout=30).read())
    if sub.get("status") != 1:
        raise HabrError(f"2captcha submit: {sub}")
    cid = sub["request"]
    for _ in range(40):
        time.sleep(5)
        r = json.loads(urllib.request.urlopen(
            f"https://2captcha.com/res.php?key={key}&action=get&id={cid}&json=1", timeout=30).read())
        if r.get("status") == 1:
            return r["request"]
        if r.get("request") != "CAPCHA_NOT_READY":
            raise HabrError(f"2captcha poll: {r}")
    raise HabrError("2captcha timeout")


async def browser_login(login, pw, key):
    """Логин через Playwright + 2captcha. -> storage_state(dict)."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(locale="ru-RU")
        pg = await ctx.new_page()
        try:
            await pg.goto(f"{BASE}/users/sign_in", wait_until="domcontentloaded")
            await pg.wait_for_timeout(2500)
            sitekey = await pg.evaluate(
                "()=>{const d=document.querySelector('[data-sitekey]');return d?d.getAttribute('data-sitekey'):"
                "(document.documentElement.innerHTML.match(/sitekey[\"':\\s]+([A-Za-z0-9_-]{20,})/)||[])[1];}")
            if not sitekey:
                raise HabrError("sitekey не найден на странице логина")
            token = await asyncio.to_thread(_2captcha, key, sitekey, pg.url)
            await pg.locator("input[type=email],input[name='email']").first.fill(login)
            await pg.locator("input[type=password]").first.fill(pw)
            await pg.evaluate(
                "(t)=>{let i=document.querySelector('input[name=\"smart-token\"]');"
                "if(!i){i=document.createElement('input');i.type='hidden';i.name='smart-token';"
                "document.querySelector('form').appendChild(i);}i.value=t;}", token)
            await pg.locator("button[type=submit],input[type=submit]").first.click()
            await pg.wait_for_timeout(7000)
            if "sign_in" in pg.url or "ident" in pg.url:
                raise HabrError("логин не прошёл (неверные креды или капча не принята)")
            return await ctx.storage_state()
        finally:
            await b.close()


class HabrAPI:
    def __init__(self, account):
        self.account = account
        self._hc = None

    def _cookies(self):
        raw = pgconn.get_setting("habr.session", account=self.account)
        if not raw:
            return {}
        ss = json.loads(raw)
        return {c["name"]: c["value"] for c in ss.get("cookies", [])
                if "career.habr" in c.get("domain", "")}

    def _client(self):
        if self._hc is None:
            self._hc = httpx.AsyncClient(base_url=BASE, cookies=self._cookies(),
                                         headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
        return self._hc

    async def aclose(self):
        if self._hc is not None:
            await self._hc.aclose()
            self._hc = None

    async def _authed(self):
        try:
            r = await self._client().get("/")
            return "sign_out" in r.text
        except Exception:
            return False

    async def ensure_auth(self):
        """Сессия жива -> ок; иначе релогин (браузер + 2captcha) и сохранить cookies."""
        if self._cookies() and await self._authed():
            return
        key = pgconn.get_setting("habr.2captcha_key", account=self.account)
        login = pgconn.get_setting("habr.login", account=self.account)
        pwenc = pgconn.get_setting("habr.password", account=self.account)
        if not (key and login and pwenc):
            raise HabrError("нет habr.login / habr.password / habr.2captcha_key")
        ss = await browser_login(login, pgconn.dec_session(pwenc), key)
        pgconn.set_setting("habr.session", json.dumps(ss), account=self.account)
        await self.aclose()  # пересоздать клиент со свежими cookies

    async def offers(self, **filters):
        """Вакансии (публичный JSON). filters: q, skills[], specializations[], qid, remote, salary…"""
        params = {"type": "all", "page": 1, **filters}
        r = await self._client().get("/api/frontend/vacancies", params=params)
        return (r.json() or {}).get("list", [])

    async def _csrf(self):
        r = await self._client().get("/")
        m = (re.search(r'name="csrf-token"\s+content="([^"]+)"', r.text)
             or re.search(r'content="([^"]+)"\s+name="csrf-token"', r.text))
        if not m:
            raise HabrError("CSRF не найден (возможно сессия протухла)")
        return m.group(1)

    async def apply(self, vid, cover=""):
        """Отклик: POST /api/frontend/vacancies/{id}/responses (multipart + CSRF + cookies)."""
        csrf = await self._csrf()
        r = await self._client().post(
            f"/api/frontend/vacancies/{vid}/responses",
            files={"body": (None, cover or "")},
            headers={"x-csrf-token": csrf, "x-requested-with": "XMLHttpRequest"})
        return r
