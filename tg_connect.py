"""Подключение Telegram-аккаунта кандидата (user-сессия Telethon) по QR-коду.

Это ОБЩАЯ привязка: сохранённая сессия (app_config.tg_user_session) дальше
используется любыми TG-фичами (отвечатель ГигаРекрутера и т.п.), не только гигой.

Кандидат: Telegram → Настройки → Устройства → Подключить устройство → скан QR.
QR постится в топик юзера. Запуск per-user (HH_DB_SCHEMA), обычно через run_all.

api_id/api_hash — общий публичный ключ (Telegram Desktop): идентифицирует ПРОГРАММУ,
не пользователя; кандидат логинится своим аккаунтом через QR.
"""
import asyncio
import io
import sys

import httpx
import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from hh_applicant_tool.storage import pgconn

API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
CYCLES = 6  # ~6 * 50с ожидания скана


def _qr_png(data: str) -> bytes:
    buf = io.BytesIO()
    qrcode.make(data).save(buf, format="PNG")
    return buf.getvalue()


async def _photo(token, chat_id, topic_id, png, caption):
    data = {"chat_id": str(chat_id), "caption": caption}
    if topic_id:
        data["message_thread_id"] = str(topic_id)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data=data, files={"photo": ("qr.png", png, "image/png")},
        )
        return (r.json().get("result") or {}).get("message_id")


async def _text(token, chat_id, topic_id, text):
    data = {"chat_id": str(chat_id), "text": text}
    if topic_id:
        data["message_thread_id"] = str(topic_id)
    async with httpx.AsyncClient(timeout=30) as c:
        await c.post(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )


async def _delete(token, chat_id, mid):
    if not mid:
        return
    async with httpx.AsyncClient(timeout=15) as c:
        await c.post(
            f"https://api.telegram.org/bot{token}/deleteMessage",
            data={"chat_id": str(chat_id), "message_id": str(mid)},
        )


async def main():
    cfg = pgconn.app_config()
    tg = cfg.get("telegram") or {}
    token, chat_id, topic_id = tg.get("token"), tg.get("chat_id"), tg.get("topic_id")
    if not (token and chat_id):
        print("tg_connect: нет telegram-конфига")
        return

    # уже подключён и сессия валидна?
    existing = cfg.get("tg_user_session")
    if existing:
        try:
            cl = TelegramClient(StringSession(existing), API_ID, API_HASH)
            await cl.connect()
            if await cl.is_user_authorized():
                me = await cl.get_me()
                print(f"tg_connect: уже подключён ({me.first_name}, {me.phone})")
                await cl.disconnect()
                if "--force" not in sys.argv:
                    return
            await cl.disconnect()
        except Exception:
            pass

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    mid = None
    try:
        qr = await client.qr_login()
        linked = False
        for _ in range(CYCLES):
            png = _qr_png(qr.url)
            new_mid = await _photo(
                token, chat_id, topic_id, png,
                "🔗 Подключение Telegram к боту\n\n"
                "Открой Telegram → Настройки → Устройства → «Подключить устройство» "
                "→ наведи камеру на этот QR.\n(код обновляется автоматически)",
            )
            await _delete(token, chat_id, mid)
            mid = new_mid
            try:
                await qr.wait(timeout=50)
                linked = True
                break
            except SessionPasswordNeededError:
                await _text(
                    token, chat_id, topic_id,
                    "⚠️ Включён облачный пароль (2FA). Сними его временно: Настройки "
                    "→ Конфиденциальность → Облачный пароль — и повтори подключение.",
                )
                return
            except asyncio.TimeoutError:
                await qr.recreate()
                continue

        if not linked:
            await _delete(token, chat_id, mid)
            await _text(token, chat_id, topic_id,
                        "⌛ QR истёк, никто не отсканировал. Повтори подключение.")
            return

        sess = client.session.save()
        me = await client.get_me()
        pgconn.set_app_config("tg_user_session", sess)
        await _delete(token, chat_id, mid)
        await _text(
            token, chat_id, topic_id,
            f"✅ Telegram подключён: {me.first_name}. Теперь бот сможет проходить "
            "ГигаРекрутер и работать через твой Telegram.",
        )
        print(f"tg_connect: подключён {me.phone}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
