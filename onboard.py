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
                    raise RuntimeError(
                        "веб-логин не удался (неверный логин/пароль, либо hh "
                        "запросил код/капчу)"
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
