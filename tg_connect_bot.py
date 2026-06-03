"""Бот привязки Telegram-аккаунта кандидата (aiogram 3.x). Команда /connect В ЛС:
QR-вход через Telethon -> сохранение зашифрованной user-сессии в схему юзера,
найденную по совпадению номера телефона Telegram с номером hh-профиля.

aiogram отвечает за бот-сторону (polling, хендлеры, FSM, отправка). Telethon —
за user-сессию (qr_login). 2FA (облачный пароль) обрабатывается через FSM.

Запуск (процесс, держится watchdog/startup): HH_DB_SCHEMA=u_egor python tg_connect_bot.py
"""
import asyncio
import io
import json

import psycopg
import qrcode
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, Message
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from hh_applicant_tool.storage import pgconn

API_ID, API_HASH = pgconn.tg_api()  # из /app/config/.tg_api (своё приложение)

dp = Dispatcher()
_pending: dict[int, TelegramClient] = {}  # chat_id -> клиент на шаге 2FA


class Connect(StatesGroup):
    password = State()


def _png(data: str) -> bytes:
    buf = io.BytesIO()
    qrcode.make(data).save(buf, format="PNG")
    return buf.getvalue()


def resolve_schema_by_phone(tg_phone):
    """Схема юзера, чей hh-номер совпадает с номером Telegram-аккаунта."""
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


async def _finish(message: Message, client: TelegramClient):
    """Логин завершён: матч по телефону -> сохранить зашифрованную сессию."""
    me = await client.get_me()
    schema = resolve_schema_by_phone(me.phone)
    if not schema:
        try:
            await client.log_out()
        finally:
            await client.disconnect()
        await message.answer(
            f"⚠️ Номер +{me.phone} не совпал ни с одним hh-аккаунтом в боте. "
            "Подключайся с того Telegram, чей номер = номер в твоём hh-профиле."
        )
        return
    save_session(schema, pgconn.enc_session(client.session.save()))
    await client.disconnect()
    await message.answer(
        f"✅ Telegram подключён к hh-аккаунту «{schema}». Сессия зашифрована. Готово."
    )
    print(f"linked: +{me.phone} -> {schema}")


@dp.message(Command("connect"))
async def cmd_connect(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return
    await state.clear()
    await message.answer("Генерирую QR-код…")
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    qr = await client.qr_login()
    qr_msg = None
    cap = ("🔗 Подключение Telegram\n\nTelegram → Настройки → Устройства → "
           "«Подключить устройство» → сканируй QR.")
    for _ in range(6):
        if qr_msg:
            try:
                await qr_msg.delete()
            except Exception:
                pass
        qr_msg = await message.answer_photo(
            BufferedInputFile(_png(qr.url), "qr.png"), caption=cap
        )
        try:
            await qr.wait(timeout=50)
        except SessionPasswordNeededError:
            if qr_msg:
                try:
                    await qr_msg.delete()
                except Exception:
                    pass
            _pending[message.chat.id] = client
            await state.set_state(Connect.password)
            await message.answer(
                "🔐 На аккаунте включён облачный пароль (2FA). Пришли его одним "
                "сообщением — я удалю его сразу после ввода."
            )
            return
        except asyncio.TimeoutError:
            await qr.recreate()
            continue
        # успех
        if qr_msg:
            try:
                await qr_msg.delete()
            except Exception:
                pass
        await _finish(message, client)
        return
    if qr_msg:
        try:
            await qr_msg.delete()
        except Exception:
            pass
    await client.disconnect()
    await message.answer("⌛ QR истёк, никто не отсканировал. Повтори /connect.")


@dp.message(Connect.password)
async def got_password(message: Message, state: FSMContext):
    pw = (message.text or "").strip()
    try:
        await message.delete()  # пароль из чата убираем
    except Exception:
        pass
    client = _pending.pop(message.chat.id, None)
    await state.clear()
    if not client:
        await message.answer("Сессия истекла, повтори /connect.")
        return
    try:
        await client.sign_in(password=pw)
    except Exception as e:
        await client.disconnect()
        await message.answer(
            f"❌ Пароль не подошёл ({type(e).__name__}). Повтори /connect."
        )
        return
    await _finish(message, client)


async def main():
    token = (pgconn.app_config().get("telegram") or {}).get("token")
    if not token:
        print("tg_connect_bot: нет telegram-токена")
        return
    bot = Bot(token)
    print("tg_connect_bot (aiogram): слушаю /connect в ЛС…")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
