"""Слушающий бот: команда /connect В ЛИЧКЕ -> QR-вход Telethon -> сохранение
user-сессии Telegram кандидата. Только ЛС, ничего не постится в топики/группу.

  /connect            — тест: сессия в public.tg_sessions (привязка к tg-аккаунту)
  /connect <имя>      — привязать к юзеру (имя из public.app_users) -> в его схему
                        app_config.tg_user_session (для ГигаРекрутера и пр.)

api_id/api_hash — общий публичный ключ (Telegram Desktop): идентифицирует ПРОГРАММУ,
не пользователя; кандидат логинится своим аккаунтом сканом QR.

Запуск (процесс): HH_DB_SCHEMA=u_egor python tg_connect_bot.py  (токен берётся из
telegram-конфига; он один на всех юзеров).
"""
import asyncio
import io
import json

import httpx
import psycopg
import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from hh_applicant_tool.storage import pgconn

API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"


def bot_token():
    return (pgconn.app_config().get("telegram") or {}).get("token")


def _png(data: str) -> bytes:
    buf = io.BytesIO()
    qrcode.make(data).save(buf, format="PNG")
    return buf.getvalue()


async def api(token, method, files=None, **kw):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"https://api.telegram.org/bot{token}/{method}", data=kw, files=files
        )
        return r.json()


def resolve_schema(arg):
    if not arg:
        return None
    arg = arg.strip().lower()
    for name, schema in pgconn.list_users():
        if arg in (name.lower(), schema.lower()):
            return schema
    return None


def save_session(schema, sess, tg_id, name, phone):
    conn = psycopg.connect(pgconn.get_dsn())
    try:
        with conn.cursor() as cur:
            if schema:
                cur.execute(
                    f'INSERT INTO "{schema}".app_config(key, value) '
                    "VALUES (%s, %s::jsonb) ON CONFLICT(key) DO UPDATE SET "
                    "value = excluded.value, updated_at = now()",
                    ("tg_user_session", json.dumps(sess)),
                )
            else:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS public.tg_sessions ("
                    "tg_user_id bigint PRIMARY KEY, name text, phone text, "
                    "session text, created_at timestamptz DEFAULT now())"
                )
                cur.execute(
                    "INSERT INTO public.tg_sessions(tg_user_id, name, phone, session) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT(tg_user_id) DO UPDATE SET "
                    "session = excluded.session, phone = excluded.phone, "
                    "name = excluded.name",
                    (tg_id, name, phone, sess),
                )
        conn.commit()
    finally:
        conn.close()


async def do_connect(token, chat_id, target_schema):
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    mid = None
    try:
        qr = await client.qr_login()
        linked = False
        for _ in range(6):
            res = await api(
                token, "sendPhoto", chat_id=str(chat_id),
                caption="🔗 Подключение Telegram\n\nTelegram → Настройки → "
                        "Устройства → «Подключить устройство» → сканируй QR.",
                files={"photo": ("qr.png", _png(qr.url), "image/png")},
            )
            new_mid = (res.get("result") or {}).get("message_id")
            if mid:
                await api(token, "deleteMessage", chat_id=str(chat_id),
                          message_id=str(mid))
            mid = new_mid
            try:
                await qr.wait(timeout=50)
                linked = True
                break
            except SessionPasswordNeededError:
                await api(token, "sendMessage", chat_id=str(chat_id),
                          text="⚠️ Включён облачный пароль (2FA). Сними его "
                               "временно (Настройки→Конфиденциальность→Облачный "
                               "пароль) и повтори /connect.")
                return
            except asyncio.TimeoutError:
                await qr.recreate()
                continue
        if mid:
            await api(token, "deleteMessage", chat_id=str(chat_id),
                      message_id=str(mid))
        if not linked:
            await api(token, "sendMessage", chat_id=str(chat_id),
                      text="⌛ QR истёк, никто не отсканировал. Повтори /connect.")
            return
        sess = client.session.save()
        me = await client.get_me()
        save_session(target_schema, sess, chat_id, me.first_name, me.phone)
        tgt = f"к «{target_schema}»" if target_schema else "(тестовая привязка)"
        await api(token, "sendMessage", chat_id=str(chat_id),
                  text=f"✅ Telegram подключён: {me.first_name}. Привязка {tgt}.")
        print(f"linked: {me.phone} -> {target_schema or 'public.tg_sessions'}")
    finally:
        await client.disconnect()


async def main():
    token = bot_token()
    if not token:
        print("tg_connect_bot: нет telegram-токена")
        return
    print("tg_connect_bot: слушаю /connect в ЛС…")
    offset = 0
    busy = set()
    while True:
        try:
            res = await api(token, "getUpdates", offset=offset, timeout=25)
        except Exception:
            await asyncio.sleep(2)
            continue
        for u in res.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            chat = msg.get("chat") or {}
            text = (msg.get("text") or "").strip()
            if chat.get("type") != "private":
                continue
            if not text.lower().startswith("/connect"):
                continue
            cid = chat["id"]
            if cid in busy:
                continue
            arg = text[len("/connect"):].strip()
            schema = resolve_schema(arg)
            if arg and not schema:
                users = ", ".join(n for n, _ in pgconn.list_users())
                await api(token, "sendMessage", chat_id=str(cid),
                          text=f"Не нашёл юзера «{arg}». Доступны: {users}")
                continue
            busy.add(cid)
            try:
                await api(token, "sendMessage", chat_id=str(cid),
                          text="Генерирую QR-код…")
                await do_connect(token, cid, schema)
            finally:
                busy.discard(cid)


if __name__ == "__main__":
    asyncio.run(main())
