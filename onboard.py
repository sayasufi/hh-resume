"""Онбординг нового hh-аккаунта ОДНИМ веб-логином (email+пароль):
  -> web_state (куки hh для apply_tests / браузер)
  -> OAuth-токен (для API: apply-similar, reply-employers и т.д.)
плюс провижин схемы/роли/таблиц и дефолты. Вызывается из бота (/addaccount).

Идея: один логин в hh-вебе даёт куки (web_state), а затем открытие OAuth-URL под
активной сессией авто-подтверждается и редиректит на hhandroid://...?code= -> токен.
"""
import asyncio
import json
import re
from urllib.parse import parse_qs, urlsplit

import psycopg
from playwright.async_api import async_playwright

from apply_tests import web_login  # переиспользуем проверенные селекторы логина
from hh_applicant_tool.api.client import ApiClient, OAuthClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn

HH_SCHEME = "hhandroid"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class OnboardError(RuntimeError):
    """Ошибка онбординга с опциональным скриншотом экрана hh (для диагностики)."""

    def __init__(self, message, screenshot: bytes | None = None):
        super().__init__(message)
        self.screenshot = screenshot


class LoginSession:
    """Поэтапный веб-логин hh с ЖИВЫМ браузером между сообщениями бота.

    Новый UI hh (2026): экран выбора соискатель/работодатель -> «Войти» ->
    вкладки Телефон/Почта. Телефон -> код по SMS; почта+пароль -> hh часто
    тоже требует код подтверждения. Поэтому держим страницу открытой и
    спрашиваем код у пользователя.

    Поток: start(login, password) -> 'need_code' | 'logged_in';
    при 'need_code' -> submit_code(code) -> 'logged_in'; затем finalize().
    """

    LOGIN_URL = "https://hh.ru/account/login"

    def __init__(self):
        self._cm = None
        self.p = None
        self.browser = None
        self.ctx = None
        self.page = None
        self.oauth = None
        self.login = ""
        self.is_phone = False
        self.web_state = None

    async def _shot(self):
        try:
            return await self.page.screenshot()
        except Exception:
            return None

    async def _fill(self, sels, val) -> bool:
        for sel in sels:
            el = await self.page.query_selector(sel)
            if el:
                await el.fill(val)
                return True
        return False

    async def _click_submit(self) -> None:
        for sel in ('button[data-qa="submit-button"]',
                    'button[data-qa="account-login-submit"]',
                    'button[type="submit"]'):
            el = await self.page.query_selector(sel)
            if el:
                try:
                    await el.click()
                    return
                except Exception:
                    continue

    async def _code_input(self):
        for sel in ('input[data-qa="otp-code-input"]',
                    'input[data-qa="account-login-by-code-input"]',
                    'input[data-qa="magritte-otp-input"]',
                    'input[autocomplete="one-time-code"]',
                    'input[inputmode="numeric"]',
                    'input[name*="code" i]'):
            el = await self.page.query_selector(sel)
            if el:
                return el
        # фолбэк: первое видимое текстовое поле (не username/password)
        for el in await self.page.query_selector_all("input"):
            try:
                t = (await el.get_attribute("type")) or "text"
                if t in ("hidden", "checkbox", "radio", "password"):
                    continue
                nm = (await el.get_attribute("name")) or ""
                if nm in ("username", "password"):
                    continue
                if await el.is_visible():
                    return el
            except Exception:
                continue
        return None

    async def _has_captcha(self) -> bool:
        return bool(await self.page.query_selector(
            'input[data-qa="account-captcha-input"], '
            'img[data-qa="account-captcha-picture"]'
        ))

    async def _advance_to_credentials(self) -> None:
        await self.page.goto(self.LOGIN_URL, timeout=40000,
                             wait_until="domcontentloaded")
        await self.page.wait_for_timeout(1500)
        # экран 1: карточки соискатель/работодатель -> «Войти» (submit-button)
        cred = ('input[data-qa="credential-type-PHONE"], '
                'input[data-qa="credential-type-EMAIL"]')
        if not await self.page.query_selector(cred):
            sb = await self.page.query_selector('button[data-qa="submit-button"]')
            if sb:
                await sb.click()
                await self.page.wait_for_timeout(2500)

    async def _enter_email(self, email, password) -> None:
        r = await self.page.query_selector('input[data-qa="credential-type-EMAIL"]')
        if r:
            await r.click(force=True)
            await self.page.wait_for_timeout(700)
        if not await self._fill(
            ['input[data-qa="applicant-login-input-email"]', 'input[name="username"]'],
            email,
        ):
            raise OnboardError("не нашёл поле email на странице hh", await self._shot())
        exp = await self.page.query_selector('button[data-qa="expand-login-by-password"]')
        if exp:
            try:
                await exp.click()
                await self.page.wait_for_timeout(1200)
            except Exception:
                pass
        if not await self._fill(
            ['input[data-qa="applicant-login-input-password"]',
             'input[name="password"]', 'input[type="password"]'],
            password,
        ):
            raise OnboardError(
                "не нашёл поле пароля — hh мог сразу запросить вход по коду.",
                await self._shot(),
            )
        await self._click_submit()

    async def _enter_phone(self, phone) -> None:
        r = await self.page.query_selector('input[data-qa="credential-type-PHONE"]')
        if r:
            await r.click(force=True)
            await self.page.wait_for_timeout(700)
        digits = re.sub(r"\D", "", phone)
        if len(digits) == 11 and digits[0] in "78":
            digits = digits[1:]
        nat = await self.page.query_selector(
            'input[data-qa="magritte-phone-input-national-number-input"]'
        )
        if not nat:
            raise OnboardError("не нашёл поле телефона на странице hh", await self._shot())
        await nat.click()
        await nat.fill(digits)
        await self.page.wait_for_timeout(500)
        await self._click_submit()  # «Дальше» -> отправляет код по SMS

    async def _settle(self) -> None:
        """Ждём до ~9с: появился код / ушли со страницы логина / капча."""
        for _ in range(18):
            await self.page.wait_for_timeout(500)
            if await self._code_input():
                return
            if "login" not in self.page.url.lower():
                return
            if await self._has_captcha():
                return

    async def _state(self) -> str:
        if await self._has_captcha():
            raise OnboardError("hh показал капчу — повтори позже.", await self._shot())
        url = self.page.url.lower()
        if await self._code_input() or "otp" in url or "code" in url:
            return "need_code"
        if "login" in url:
            raise OnboardError(
                "неверный логин/пароль — hh не пустил дальше.", await self._shot()
            )
        return "logged_in"

    async def start(self, login, password) -> str:
        login = (login or "").strip()
        self.login = login
        self.is_phone = "@" not in login
        self._cm = async_playwright()
        self.p = await self._cm.__aenter__()
        self.browser = await self.p.chromium.launch(headless=True)
        self.ctx = await self.browser.new_context(locale="ru-RU", user_agent=_UA)
        self.page = await self.ctx.new_page()
        self.oauth = OAuthClient(user_agent=generate_android_useragent())
        await self._advance_to_credentials()
        if self.is_phone:
            await self._enter_phone(login)
        else:
            await self._enter_email(login, password)
        await self._settle()
        return await self._state()

    async def submit_code(self, code) -> str:
        ci = await self._code_input()
        if not ci:
            raise OnboardError("не нашёл поле для кода на странице hh.", await self._shot())
        digits = re.sub(r"\D", "", code) or (code or "").strip()
        await ci.click()
        try:
            await ci.fill("")
        except Exception:
            pass
        await self.page.keyboard.type(digits)  # одно поле или 6 ячеек
        await self.page.wait_for_timeout(800)
        await self._click_submit()
        await self._settle()
        if await self._has_captcha():
            raise OnboardError("hh показал капчу — повтори позже.", await self._shot())
        if await self._code_input() or "otp" in self.page.url.lower():
            raise OnboardError(
                "код не подошёл — проверь и повтори /addaccount.", await self._shot()
            )
        return "logged_in"

    async def finalize(self):
        """web_state (куки) + OAuth-токен + me + resumes."""
        self.web_state = await self.ctx.storage_state()
        fut = asyncio.get_event_loop().create_future()

        def on_req(req):
            if req.url.startswith(HH_SCHEME + "://") and not fut.done():
                c = parse_qs(urlsplit(req.url).query).get("code", [None])[0]
                if c:
                    fut.set_result(c)

        self.page.on("request", on_req)
        await self.page.goto(self.oauth.authorize_url, wait_until="load", timeout=30000)
        try:
            code = await asyncio.wait_for(fut, timeout=25)
        except asyncio.TimeoutError:
            raise OnboardError(
                "не удалось получить OAuth-код (нужна доп. авторизация приложения).",
                await self._shot(),
            )
        token = await self.oauth.authenticate(code)
        api = ApiClient(
            access_token=token["access_token"], refresh_token=token["refresh_token"],
            access_expires_at=token["access_expires_at"],
            user_agent=generate_android_useragent(),
        )
        try:
            me = await api.get("/me")
            resumes = (await api.get("/resumes/mine")).get("items", [])
        finally:
            await api.aclose()
        return token, self.web_state, me, resumes

    async def close(self) -> None:
        for coro in (
            (self.oauth.aclose() if self.oauth else None),
            (self.browser.close() if self.browser else None),
        ):
            if coro is not None:
                try:
                    await coro
                except Exception:
                    pass
        if self._cm is not None:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception:
                pass


async def authorize_hh(login: str, password: str):
    """Веб-логин -> (token, web_state, me, resumes). RuntimeError при неудаче."""
    oauth = OAuthClient(user_agent=generate_android_useragent())
    token = None
    web_state = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(locale="ru-RU", user_agent=_UA)
            page = await ctx.new_page()
            try:
                if not await web_login(page, login, password):
                    url = page.url.lower()
                    otp = ("otp" in url) or bool(await page.query_selector(
                        'input[data-qa="otp-code-input"], '
                        '[data-qa="account-login-code-input"], '
                        'input[name="otpCode"]'
                    ))
                    captcha = bool(await page.query_selector(
                        'input[data-qa="account-captcha-input"], '
                        'img[data-qa="account-captcha-picture"]'
                    ))
                    if otp:
                        raise RuntimeError(
                            "hh запросил вход по КОДУ (SMS/почта) — так бывает при "
                            "входе по ТЕЛЕФОНУ. Вход по коду пока не поддержан: "
                            "укажи EMAIL аккаунта и его пароль."
                        )
                    if captcha:
                        raise RuntimeError("hh показал капчу — повтори позже.")
                    raise RuntimeError(
                        "неверный логин/пароль. Если вводил телефон — попробуй "
                        "EMAIL аккаунта (вход по телефону требует SMS-кода)."
                    )
                web_state = await ctx.storage_state()
                fut = asyncio.get_event_loop().create_future()

                def on_req(req):
                    if req.url.startswith(HH_SCHEME + "://") and not fut.done():
                        c = parse_qs(urlsplit(req.url).query).get("code", [None])[0]
                        if c:
                            fut.set_result(c)

                page.on("request", on_req)
                await page.goto(oauth.authorize_url, wait_until="load", timeout=30000)
                try:
                    code = await asyncio.wait_for(fut, timeout=25)
                except asyncio.TimeoutError:
                    raise RuntimeError(
                        "не удалось получить OAuth-код (возможно, нужна доп. "
                        "авторизация приложения на hh)"
                    )
                token = await oauth.authenticate(code)
            finally:
                await browser.close()
    finally:
        await oauth.aclose()

    api = ApiClient(
        access_token=token["access_token"], refresh_token=token["refresh_token"],
        access_expires_at=token["access_expires_at"],
        user_agent=generate_android_useragent(),
    )
    try:
        me = await api.get("/me")
        resumes = (await api.get("/resumes/mine")).get("items", [])
    finally:
        await api.aclose()
    return token, web_state, me, resumes


async def fetch_resume_full(token, resume_id):
    api = ApiClient(
        access_token=token["access_token"], refresh_token=token["refresh_token"],
        access_expires_at=token["access_expires_at"],
        user_agent=generate_android_useragent(),
    )
    try:
        return await api.get(f"/resumes/{resume_id}")
    finally:
        await api.aclose()


def build_resume_text(me, r):
    """Краткий resume_text для AI (письма/ответы) из hh-резюме."""
    L = []
    name = " ".join(
        x for x in [me.get("last_name"), me.get("first_name"),
                    me.get("middle_name")] if x
    )
    if name:
        L.append(name)
    if r.get("title"):
        L.append("Должность: " + r["title"])
    area = (r.get("area") or {}).get("name")
    if area:
        L.append("Город: " + area)
    ss = r.get("skill_set") or []
    if ss:
        L.append("Навыки: " + ", ".join(ss[:40]))
    elif r.get("skills"):
        L.append("Навыки: " + str(r["skills"])[:500])
    exp = r.get("experience") or []
    if exp:
        L.append("Опыт работы:")
        for e in exp[:6]:
            line = f"- {e.get('position','') or ''} в {e.get('company','') or ''}"
            st = (e.get("start") or "")[:7]
            if st:
                line += f" ({st}–{(e.get('end') or 'н.в.')[:7]})"
            L.append(line)
            d = re.sub(r"<[^>]+>", "", (e.get("description") or "")).strip()
            if d:
                L.append("  " + d[:400])
    return "\n".join(L)


def setup_account(name, account, login, password, token, web_state, me,
                  resume_id, resume_text, salary, topic_id, bot_token, chat_id):
    """Регистрация аккаунта + запись токена/web_state/дефолтов в общую схему
    (разделение по колонке account). Без отдельной схемы/роли."""
    full_name = " ".join(
        x for x in [me.get("last_name"), me.get("first_name"),
                    me.get("middle_name")] if x
    ) or name

    pgconn.register_user(full_name, account)  # connect(ensure=True) создаёт таблицы

    conn = psycopg.connect(pgconn.get_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            # openai-конфиг копируем с любого существующего аккаунта (общий vLLM)
            cur.execute("SELECT value FROM app_config WHERE key='openai' LIMIT 1")
            row = cur.fetchone()
            openai_cfg = row[0] if row else None

            def setcfg(k, v):
                cur.execute(
                    "INSERT INTO app_config(account, key, value) VALUES (%s, %s, %s::jsonb) "
                    "ON CONFLICT(account, key) DO UPDATE SET value=excluded.value, updated_at=now()",
                    (account, k, json.dumps(v)),
                )

            def setset(k, v):
                cur.execute(
                    "INSERT INTO settings(account, key, value) VALUES (%s, %s, %s) "
                    "ON CONFLICT(account, key) DO UPDATE SET value=excluded.value",
                    (account, k, json.dumps(v)),
                )

            setcfg("token", token)
            setcfg("web_state", web_state)
            setcfg("resume_text", resume_text)
            setcfg("hh_phone", me.get("phone") or "")
            if openai_cfg:
                setcfg("openai", openai_cfg)
            setcfg("preferences", {"salary": salary})
            tg = {"token": bot_token, "chat_id": chat_id}
            if topic_id:
                tg["topic_id"] = topic_id
            setcfg("telegram", tg)

            setset("auth.username", login)
            setset("auth.password", password)
            setset("apply.use_ai", True)
            setset("apply.force_message", True)
            setset("apply.max_per_day", 15)
            setset("apply.resume_id", resume_id)
            setset("user.full_name", full_name)
        conn.commit()
    finally:
        conn.close()
    return full_name
