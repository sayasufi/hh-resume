"""Слушающий бот: команда /connect В ЛИЧКЕ -> QR-вход Telethon -> сохранение
user-сессии Telegram кандидата. Только ЛС, ничего не постится в топики/группу.

Сопоставление Telegram<->hh — АВТОМАТИЧЕСКИ по номеру телефона: после входа
берём номер Telegram-аккаунта и ищем hh-аккаунт (схему) с таким же номером в
профиле (app_config.hh_phone, кладёт monitor.py из hh /me). Совпало -> сессия
(ЗАШИФРОВАННАЯ) сохраняется в схему этого юзера: u_xxx.app_config.tg_user_session.
Не совпало -> сессию не храним, просто сообщаем (и разлогиниваем устройство).

api_id/api_hash — общий публичный ключ (Telegram Desktop): идентификатор ПРОГРАММЫ,
не пользователя; кандидат логинится своим аккаунтом сканом QR.

Запуск (процесс): HH_DB_SCHEMA=u_egor python tg_connect_bot.py  (токен бота один
на всех; берётся из telegram-конфига).
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


def resolve_schema_by_phone(tg_phone):
    """Найти схему юзера, чей hh-номер совпадает с номером Telegram-аккаунта."""
    norm = pgconn._norm_phone(tg_phone)
    if not norm:
        return None
    conn = psycopg.connect(pgconn.get_dsn())
    try:
        with conn.cursor() as cur:
            for _name, schema in pgconn.list_users():
                cur.execute(
                    f'SELECT value FROM "{schema}".app_config WHERE key = %s',
                    ("hh_phone",),
                )
                row = cur.fetchone()
                if row and pgconn._norm_phone(row[0]) == norm:
                    return schema
    finally:
        conn.close()
    return None


def save_session(schema, enc_sess):
    conn = psycopg.connect(pgconn.get_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO "{schema}".app_config(key, value) '
                "VALUES (%s, %s::jsonb) ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, updated_at = now()",
                ("tg_user_session", json.dumps(enc_sess)),
            )
        conn.commit()
    finally:
        conn.close()


async def do_connect(token, chat_id):
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

        me = await client.get_me()
        schema = resolve_schema_by_phone(me.phone)
        if not schema:
            # сессию не сохраняем и разлогиниваем устройство (гигиена)
            try:
                await client.log_out()
            except Exception:
                pass
            await api(token, "sendMessage", chat_id=str(chat_id),
                      text=f"⚠️ Номер +{me.phone} не совпал ни с одним hh-аккаунтом "
                           "в боте. Подключайся с того Telegram, чей номер = номер "
                           "в твоём hh-профиле.")
            return
        save_session(schema, pgconn.enc_session(client.session.save()))
        await api(token, "sendMessage", chat_id=str(chat_id),
                  text=f"✅ Telegram подключён к hh-аккаунту «{schema}». "
                       "Сессия зашифрована. Готово.")
        print(f"linked: +{me.phone} -> {schema}")
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
            text = (msg.get("text") or "").strip().lower()
            if chat.get("type") != "private":
                continue
            if not text.startswith("/connect"):
                continue
            cid = chat["id"]
            if cid in busy:
                continue
            busy.add(cid)
            try:
                await api(token, "sendMessage", chat_id=str(cid),
                          text="Генерирую QR-код…")
                await do_connect(token, cid)
            finally:
                busy.discard(cid)


if __name__ == "__main__":
    asyncio.run(main())
