"""Бот-помощник (aiogram 3.x): меню, /start, /connect (привязка Telegram по QR),
/status, /help. Привязка Telegram нужна для авто-интервью (ГигаРекрутер) — сессия
сохраняется зашифрованной в схему юзера, найденную по совпадению номера телефона.

aiogram — бот-сторона; Telethon — user-сессия (qr_login). 2FA через FSM.
Запуск (watchdog/startup): HH_DB_SCHEMA=u_egor python tg_connect_bot.py
"""
import asyncio
import io
import json
import time

import psycopg
import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from hh_applicant_tool.storage import pgconn

API_ID, API_HASH = pgconn.tg_api()

dp = Dispatcher()
_pending: dict[int, TelegramClient] = {}  # chat_id -> клиент на шаге 2FA


class Connect(StatesGroup):
    password = State()


START_TEXT = (
    "👋 Привет! Я бот-помощник по поиску работы на hh.ru.\n\n"
    "Что я делаю сам:\n"
    "• откликаюсь на подходящие вакансии с сопроводительными письмами\n"
    "• отвечаю работодателям в чатах\n"
    "• прохожу тесты к вакансиям\n"
    "• присылаю тебе важное: приглашения на интервью, просьбы связаться\n\n"
    "Чтобы я мог проходить за тебя авто-интервью (ГигаРекрутер Сбера и т.п.) — "
    "подключи свой Telegram кнопкой ниже."
)
HELP_TEXT = (
    "❓ Команды:\n"
    "/connect — подключить твой Telegram (скан QR) для авто-интервью\n"
    "/status — статус: аккаунт, отклики, приглашения, токен\n"
    "/start — это меню\n\n"
    "Важное (интервью, контакты от работодателей) приходит автоматически "
    "приоритизированным дайджестом 🔴🟡🟢."
)


def _kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Подключить Telegram", callback_data="connect")],
        [InlineKeyboardButton(text="📊 Статус", callback_data="status"),
         InlineKeyboardButton(text="❓ Помощь", callback_data="help")],
    ])


def _png(data: str) -> bytes:
    buf = io.BytesIO()
    qrcode.make(data).save(buf, format="PNG")
    return buf.getvalue()


# --- сопоставление и хранилище ---

def _schema_by(col_key, value):
    """Найти схему, где app_config[col_key] совпадает (по нормализации для phone)."""
    conn = psycopg.connect(pgconn.get_dsn())
    try:
        with conn.cursor() as cur:
            for _n, schema in pgconn.list_users():
                cur.execute(
                    f'SELECT value FROM "{schema}".app_config WHERE key = %s',
                    (col_key,),
                )
                row = cur.fetchone()
                if not row:
                    continue
                if col_key == "hh_phone":
                    if pgconn._norm_phone(row[0]) == pgconn._norm_phone(value):
                        return schema
                elif str(row[0]) == str(value):
                    return schema
    finally:
        conn.close()
    return None


def save_link(schema, enc_sess, tg_id):
    conn = psycopg.connect(pgconn.get_dsn())
    try:
        with conn.cursor() as cur:
            for k, v in (("tg_user_session", enc_sess), ("tg_user_id", tg_id)):
                cur.execute(
                    f'INSERT INTO "{schema}".app_config(key, value) '
                    "VALUES (%s, %s::jsonb) ON CONFLICT(key) DO UPDATE SET "
                    "value = excluded.value, updated_at = now()",
                    (k, json.dumps(v)),
                )
        conn.commit()
    finally:
        conn.close()


def status_text(schema):
    conn = psycopg.connect(pgconn.get_dsn())
    g = {}
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT value FROM "{schema}".app_config WHERE key=%s', ("token",))
            r = cur.fetchone()
            tok = r[0] if r else None
            for k in ("_applications_count", "_applications_date",
                      "_applications_pause_until", "user.full_name"):
                cur.execute(f'SELECT value FROM "{schema}".settings WHERE key=%s', (k,))
                r = cur.fetchone()
                try:
                    g[k] = json.loads(r[0]) if r else None
                except Exception:
                    g[k] = r[0] if r else None
    finally:
        conn.close()

    name = g.get("user.full_name") or schema
    today = time.strftime("%Y-%m-%d")
    cnt = g.get("_applications_count") if g.get("_applications_date") == today else 0
    pause = g.get("_applications_pause_until")
    days = ((tok or {}).get("access_expires_at", 0) - time.time()) / 86400 if tok else -1
    tline = f"ок ({days:.0f} дн)" if days > 0 else "🔴 истёк — нужна переавторизация"
    lines = [
        f"📊 Статус — {name}",
        f"🔑 Токен: {tline}",
        f"📨 Откликов сегодня: {cnt}"
        + (f"  (лимит, пауза до {pause})" if pause and pause > today else ""),
        "🔗 Telegram: подключён ✅",
    ]
    return "\n".join(lines)


# --- QR-привязка ---

async def _finish(message: Message, client: TelegramClient):
    me = await client.get_me()
    schema = _schema_by("hh_phone", me.phone)
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
    save_link(schema, pgconn.enc_session(client.session.save()), me.id)
    await client.disconnect()
    await message.answer(
        f"✅ Telegram подключён к hh-аккаунту «{schema}». Сессия зашифрована.\n"
        "Теперь я смогу проходить за тебя авто-интервью."
    )
    print(f"linked: +{me.phone} -> {schema}")


async def start_connect(message: Message, state: FSMContext):
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


# --- хендлеры ---

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return
    await state.clear()
    await message.answer(START_TEXT, reply_markup=_kb())


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT)


@dp.message(Command("connect"))
async def cmd_connect(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return
    await start_connect(message, state)


@dp.message(Command("status"))
async def cmd_status(message: Message):
    if message.chat.type != "private":
        return
    schema = _schema_by("tg_user_id", message.from_user.id)
    if not schema:
        await message.answer("Твой Telegram пока не подключён. Нажми /connect.")
        return
    await message.answer(status_text(schema))


@dp.message(Connect.password)
async def got_password(message: Message, state: FSMContext):
    pw = (message.text or "").strip()
    try:
        await message.delete()
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
        await message.answer(f"❌ Пароль не подошёл ({type(e).__name__}). Повтори /connect.")
        return
    await _finish(message, client)


@dp.callback_query(F.data == "connect")
async def cb_connect(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await start_connect(cq.message, state)


@dp.callback_query(F.data == "status")
async def cb_status(cq: CallbackQuery):
    await cq.answer()
    schema = _schema_by("tg_user_id", cq.from_user.id)
    if not schema:
        await cq.message.answer("Твой Telegram пока не подключён. Нажми /connect.")
        return
    await cq.message.answer(status_text(schema))


@dp.callback_query(F.data == "help")
async def cb_help(cq: CallbackQuery):
    await cq.answer()
    await cq.message.answer(HELP_TEXT)


async def main():
    token = (pgconn.app_config().get("telegram") or {}).get("token")
    if not token:
        print("tg_connect_bot: нет telegram-токена")
        return
    bot = Bot(token)
    await bot.set_my_commands([
        BotCommand(command="start", description="О боте и быстрые действия"),
        BotCommand(command="connect", description="Подключить Telegram (QR)"),
        BotCommand(command="status", description="Статус: отклики, приглашения, токен"),
        BotCommand(command="help", description="Помощь"),
    ])
    print("tg_connect_bot (aiogram): меню установлено, слушаю команды…")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
