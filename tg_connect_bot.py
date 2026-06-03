"""Бот-помощник (aiogram 3.x): меню, /start, /connect (привязка Telegram по QR),
/status, /help. Привязка Telegram нужна для авто-интервью (ГигаРекрутер) — сессия
сохраняется зашифрованной в схему юзера, найденную по совпадению номера телефона.

aiogram — бот-сторона; Telethon — user-сессия (qr_login). 2FA через FSM.
Запуск (watchdog/startup): HH_DB_SCHEMA=u_egor python tg_connect_bot.py
"""
import asyncio
import io
import json
import re
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


class AddAcc(StatesGroup):
    login = State()
    password = State()
    code = State()
    salary = State()


_login_sessions: dict = {}  # chat_id -> onboard.LoginSession (живой браузер)


async def _drop_login(chat_id: int) -> None:
    sess = _login_sessions.pop(chat_id, None)
    if sess is not None:
        await sess.close()


async def _addacc_fail(message, state, exc) -> None:
    """Сообщить о неудаче онбординга + прислать скриншот экрана hh (диагностика)."""
    await state.clear()
    await _drop_login(message.chat.id)
    shot = getattr(exc, "screenshot", None)
    cap = f"❌ Не удалось добавить аккаунт: {exc}\nПовтори /addaccount."
    if shot:
        try:
            await message.answer_photo(
                BufferedInputFile(shot, "hh_login.png"), caption=cap[:1000]
            )
            return
        except Exception:
            pass
    await message.answer(cap)


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
    "/addaccount — добавить новый hh-аккаунт (логин/пароль hh, один раз)\n"
    "/connect — подключить твой Telegram (скан QR) для авто-интервью\n"
    "/status — статус: аккаунт, отклики, токен\n"
    "/start — это меню\n\n"
    "Важное (интервью, контакты от работодателей) приходит автоматически "
    "приоритизированным дайджестом 🔴🟡🟢."
)


def _kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить hh-аккаунт", callback_data="addacc")],
        [InlineKeyboardButton(text="🔗 Подключить Telegram", callback_data="connect")],
        [InlineKeyboardButton(text="📊 Статус", callback_data="status"),
         InlineKeyboardButton(text="❓ Помощь", callback_data="help")],
    ])


def _png(data: str) -> bytes:
    buf = io.BytesIO()
    qrcode.make(data).save(buf, format="PNG")
    return buf.getvalue()


# --- сопоставление и хранилище ---

def _account_by(col_key, value):
    """Найти account, у которого app_config[col_key] совпадает (single schema)."""
    conn = psycopg.connect(pgconn.get_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(
                "SELECT account, value FROM app_config WHERE key=%s", (col_key,)
            )
            for acc, val in cur.fetchall():
                if col_key == "hh_phone":
                    if pgconn._norm_phone(val) == pgconn._norm_phone(value):
                        return acc
                elif str(val) == str(value):
                    return acc
    finally:
        conn.close()
    return None


def save_link(account, enc_sess, tg_id):
    conn = psycopg.connect(pgconn.get_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            for k, v in (("tg_user_session", enc_sess), ("tg_user_id", tg_id)):
                cur.execute(
                    "INSERT INTO app_config(account, key, value) "
                    "VALUES (%s, %s, %s::jsonb) ON CONFLICT(account, key) DO UPDATE "
                    "SET value=excluded.value, updated_at=now()",
                    (account, k, json.dumps(v)),
                )
        conn.commit()
    finally:
        conn.close()


def status_text(account):
    conn = psycopg.connect(pgconn.get_dsn())
    g = {}
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(
                "SELECT value FROM app_config WHERE account=%s AND key='token'",
                (account,),
            )
            r = cur.fetchone()
            tok = r[0] if r else None
            for k in ("_applications_count", "_applications_date",
                      "_applications_pause_until", "user.full_name"):
                cur.execute(
                    "SELECT value FROM settings WHERE account=%s AND key=%s",
                    (account, k),
                )
                r = cur.fetchone()
                try:
                    g[k] = json.loads(r[0]) if r else None
                except Exception:
                    g[k] = r[0] if r else None
    finally:
        conn.close()

    name = g.get("user.full_name") or account
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
    account = _account_by("hh_phone", me.phone)
    if not account:
        try:
            await client.log_out()
        finally:
            await client.disconnect()
        await message.answer(
            f"⚠️ Номер +{me.phone} не совпал ни с одним hh-аккаунтом в боте. "
            "Подключайся с того Telegram, чей номер = номер в твоём hh-профиле."
        )
        return
    save_link(account, pgconn.enc_session(client.session.save()), me.id)
    await client.disconnect()
    await message.answer(
        f"✅ Telegram подключён к hh-аккаунту «{account}». Сессия зашифрована.\n"
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
    schema = _account_by("tg_user_id", message.from_user.id)
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


@dp.message(Command("addaccount"))
async def cmd_addaccount(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return
    await _drop_login(message.chat.id)
    await state.set_state(AddAcc.login)
    await message.answer(
        "➕ Новый hh-аккаунт.\nЛогин hh — email или телефон (напр. +79991234567):"
    )


@dp.message(AddAcc.login)
async def acc_login(message: Message, state: FSMContext):
    import onboard
    login = (message.text or "").strip()
    await state.update_data(login=login, password="")
    if "@" in login:  # email -> спросим пароль
        await state.set_state(AddAcc.password)
        await message.answer("Пароль hh (удалю сообщение сразу):")
        return
    # телефон -> сразу открываем браузер и отправляем код по SMS
    await message.answer("⏳ Открываю hh и отправляю код по SMS… ~минуту.")
    sess = onboard.LoginSession()
    _login_sessions[message.chat.id] = sess
    try:
        st = await sess.start(login, "")
    except Exception as e:
        await _addacc_fail(message, state, e)
        return
    if st == "need_code":
        await state.set_state(AddAcc.code)
        await message.answer("📲 Введи код из SMS:")
    else:
        await state.set_state(AddAcc.salary)
        await message.answer("Желаемая зарплата (напр. 200 000–300 000 ₽):")


@dp.message(AddAcc.password)
async def acc_password(message: Message, state: FSMContext):
    import onboard
    pw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    d = await state.get_data()
    await state.update_data(password=pw)
    await message.answer("⏳ Авторизую hh через браузер… ~минуту, подожди.")
    sess = onboard.LoginSession()
    _login_sessions[message.chat.id] = sess
    try:
        st = await sess.start(d["login"], pw)
    except Exception as e:
        await _addacc_fail(message, state, e)
        return
    if st == "need_code":
        await state.set_state(AddAcc.code)
        await message.answer(
            "📩 hh запросил код подтверждения. Введи код (SMS / почта / приложение):"
        )
    else:
        await state.set_state(AddAcc.salary)
        await message.answer("Желаемая зарплата (напр. 200 000–300 000 ₽):")


@dp.message(AddAcc.code)
async def acc_code(message: Message, state: FSMContext):
    code = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    sess = _login_sessions.get(message.chat.id)
    if sess is None:
        await state.clear()
        await message.answer("Сессия истекла. Повтори /addaccount.")
        return
    await message.answer("⏳ Проверяю код…")
    try:
        await sess.submit_code(code)
    except Exception as e:
        await _addacc_fail(message, state, e)
        return
    await state.set_state(AddAcc.salary)
    await message.answer("Желаемая зарплата (напр. 200 000–300 000 ₽):")


@dp.message(AddAcc.salary)
async def acc_salary(message: Message, state: FSMContext):
    import onboard
    d = await state.get_data()
    salary = (message.text or "").strip()
    sess = _login_sessions.get(message.chat.id)
    if sess is None:
        await state.clear()
        await message.answer("Сессия истекла. Повтори /addaccount.")
        return
    await state.clear()
    await message.answer("⏳ Завершаю авторизацию hh…")
    try:
        token, web_state, me, resumes = await sess.finalize()
    except Exception as e:
        await _addacc_fail(message, state, e)
        return
    await _drop_login(message.chat.id)  # данные получены — браузер больше не нужен
    pub = [r for r in resumes
           if (r.get("status") or {}).get("id") == "published"] or resumes
    if not pub:
        await message.answer("❌ У аккаунта нет резюме на hh. Создай и повтори.")
        return
    resume_id = pub[0]["id"]
    # идентичность — из hh-профиля (id/телефон + имя), без ника
    acc_id = str(me.get("id") or pgconn._norm_phone(me.get("phone")) or "")
    if not acc_id:
        await message.answer("❌ Не удалось определить идентификатор hh-аккаунта.")
        return
    account = re.sub(r"\W", "", acc_id)
    full_name = " ".join(
        x for x in [me.get("last_name"), me.get("first_name")] if x
    ) or d["login"]
    tg = pgconn.app_config().get("telegram") or {}
    topic_id = None
    try:
        ft = await message.bot.create_forum_topic(tg["chat_id"], full_name[:40] or "new")
        topic_id = ft.message_thread_id
    except Exception as e:
        print("create_forum_topic:", repr(e)[:80])
    try:
        full = await onboard.fetch_resume_full(token, resume_id)
        resume_text = onboard.build_resume_text(me, full)
        onboard.setup_account(
            full_name, account, d["login"], d["password"], token, web_state,
            me, resume_id, resume_text, salary, topic_id,
            tg.get("token"), tg.get("chat_id"),
        )
    except Exception as e:
        await message.answer(f"❌ Авторизация ок, но настройка не удалась: {e}")
        return
    await message.answer(
        f"✅ Аккаунт {full_name} добавлен — работает и API, и браузер.\n"
        f"Резюме: {pub[0].get('title','')}. Отклики пойдут по расписанию.\n\n"
        "Теперь /connect — привязать твой Telegram (авто-интервью)."
    )


@dp.callback_query(F.data == "addacc")
async def cb_addacc(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await _drop_login(cq.message.chat.id)
    await state.set_state(AddAcc.login)
    await cq.message.answer(
        "➕ Новый hh-аккаунт.\nЛогин hh — email или телефон (напр. +79991234567):"
    )


@dp.callback_query(F.data == "connect")
async def cb_connect(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await start_connect(cq.message, state)


@dp.callback_query(F.data == "status")
async def cb_status(cq: CallbackQuery):
    await cq.answer()
    schema = _account_by("tg_user_id", cq.from_user.id)
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
        BotCommand(command="addaccount", description="Добавить новый hh-аккаунт"),
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
