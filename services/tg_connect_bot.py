"""Бот-помощник (aiogram 3.x): меню, /start, /connect (привязка Telegram по QR),
/help. Привязка Telegram нужна для авто-интервью (ГигаРекрутер) — сессия
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
    KeyboardButton,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)

WEBAPP_URL = "https://tgbot-afisha.ru"
from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from hh_applicant_tool.storage import pgconn

API_ID, API_HASH = pgconn.tg_api()

dp = Dispatcher()
_pending: dict[int, dict] = {}  # chat_id -> {client, phone, hash} на шаге кода/2FA


class Connect(StatesGroup):
    phone = State()
    code = State()
    password = State()


class AddAcc(StatesGroup):
    login = State()
    password = State()
    code = State()
    salary = State()


class GmLink(StatesGroup):  # привязка GetMatch по коду из @g_jobbot
    code = State()


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
    "👋 Привет! Я ищу работу на hh.ru за тебя — на автопилоте.\n\n"
    "Что делаю сам, круглосуточно:\n"
    "• 📨 откликаюсь на подходящие вакансии с сопроводительными\n"
    "• 💬 отвечаю работодателям в чатах\n"
    "• 🧩 прохожу тесты к вакансиям\n"
    "• 📈 поднимаю резюме и захожу на вакансии для активности\n"
    "• 🔔 присылаю важное: приглашения, просьбы связаться\n\n"
    "📊 <b>Личный кабинет</b> — вся статистика и тумблеры: что включить, "
    "что выключить.\n\n"
    "<b>С чего начать:</b> поделись номером телефона кнопкой ниже — создам твой профиль. "
    "Дальше привяжешь аккаунты (hh, GetMatch) через /addaccount."
)
HELP_TEXT = (
    "❓ <b>Как пользоваться</b>\n\n"
    "📊 <b>Личный кабинет</b> — кнопка «Профиль» слева от поля ввода (или "
    "/start → «Открыть кабинет»): профиль, статистика и тумблеры функций.\n\n"
    "<b>Команды:</b>\n"
    "/start — открыть кабинет / привязать профиль по номеру\n"
    "/addaccount — привязать аккаунт: hh (логин+пароль) или GetMatch (код)\n"
    "/connect — дать доступ к Telegram для авто-функций (интервью, коды GetMatch)\n\n"
    "Важное (интервью, контакты работодателей) приходит автоматически "
    "дайджестом 🔴🟡🟢."
)


def _kb_linked():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть кабинет",
                              web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(text="➕ Привязать аккаунт", callback_data="addacc"),
         InlineKeyboardButton(text="🔗 Доступ к Telegram", callback_data="connect")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")],
    ])


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def _valid_login(s: str):
    """-> 'email' | 'phone' | None. Телефон РФ: 10-11 цифр."""
    s = (s or "").strip()
    if _EMAIL_RE.match(s):
        return "email"
    digits = re.sub(r"\D", "", s)
    if 10 <= len(digits) <= 11:
        return "phone"
    return None


START_LINKED = (
    "👋 С возвращением, <b>{name}</b>!\n\n"
    "Твой профиль привязан и я работаю. Открывай кабинет — там вся "
    "статистика и управление функциями."
)


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
    print(f"linked: +{me.phone} -> {account}")


def _connect_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 По коду (этот телефон)", callback_data="conn:code")],
        [InlineKeyboardButton(text="🖥 По QR (второе устройство)", callback_data="conn:qr")],
    ])


async def start_connect(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🔗 <b>Доступ к твоему Telegram</b> (по желанию).\n\n"
        "<b>Зачем:</b> чтобы я сам читал твои чаты с ботами и действовал за тебя —\n"
        "• проходил собеседования в чате ГигаРекрутера;\n"
        "• забирал коды входа GetMatch из @g_jobbot автоматически.\n\n"
        "<b>Что это:</b> вход в твой Telegram как новое устройство — я смогу читать и "
        "писать сообщения от твоего имени. Сессия хранится в зашифрованном виде; "
        "доступ в любой момент отзываешь сам: Telegram → Настройки → Устройства.\n"
        "Без этого основные функции (отклики hh, GetMatch по коду) работают и так.\n\n"
        "<b>Как войти?</b>\n"
        "• <b>По коду</b> — с этого телефона: пришлю запрос, Telegram даст код.\n"
        "• <b>По QR</b> — если сканируешь с другого устройства (комп/планшет).",
        reply_markup=_connect_kb(), parse_mode="HTML",
    )


def _e164(phone) -> str | None:
    """Нормализовать номер в формат +<код><номер> (РФ-эвристика)."""
    d = re.sub(r"\D", "", str(phone or ""))
    if len(d) == 10:
        d = "7" + d
    elif len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    return "+" + d if 11 <= len(d) <= 15 else None


async def _connect_send_code(message: Message, state: FSMContext, phone: str) -> None:
    """Запросить у Telegram код входа для phone, перейти к вводу кода."""
    await message.answer(f"⏳ Отправляю код входа на {phone}…",
                         reply_markup=ReplyKeyboardRemove())
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except PhoneNumberInvalidError:
        await client.disconnect()
        await state.set_state(Connect.phone)
        await message.answer("❌ Telegram не знает такой номер. Введи номер этого "
                             "Telegram вручную (напр. +79991234567):")
        return
    except Exception as e:
        await client.disconnect()
        await message.answer(f"❌ Не удалось отправить код ({type(e).__name__}). Повтори /connect.")
        return
    _pending[message.chat.id] = {"client": client, "phone": phone,
                                 "hash": sent.phone_code_hash}
    await state.set_state(Connect.code)
    await message.answer(
        "📲 Telegram прислал тебе <b>код для входа</b> (в чат «Telegram»).\n\n"
        "⚠️ <b>Важно:</b> вводи код <b>через пробелы или дефисы</b> — например, если "
        "код <code>12345</code>, напиши <b>1 2 3 4 5</b> или <b>1-2-3-4-5</b>.\n"
        "Если ввести просто «12345», Telegram аннулирует код как «пересланный в чат».",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "conn:code")
async def cb_conn_code(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    # уже привязан /link -> номер известен (hh_phone), не спрашиваем
    linked = _account_by("tg_user_id", cq.from_user.id)
    phone = _e164((pgconn.app_config(account=linked).get("hh_phone"))) if linked else None
    if phone:
        await _connect_send_code(cq.message, state, phone)
        return
    await state.set_state(Connect.phone)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться своим номером", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await cq.message.answer(
        "Нажми кнопку ниже 👇 или введи номер этого Telegram вручную "
        "(с кодом страны, напр. +79991234567):", reply_markup=kb,
    )


@dp.callback_query(F.data == "conn:qr")
async def cb_conn_qr(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await _connect_qr(cq.message, state)


@dp.message(Connect.phone)
async def conn_got_phone(message: Message, state: FSMContext):
    if message.contact:  # поделился номером кнопкой
        if message.contact.user_id and message.contact.user_id != message.from_user.id:
            await message.answer("Это чужой контакт. Поделись СВОИМ номером.")
            return
        raw = message.contact.phone_number or ""
    else:
        raw = (message.text or "").strip()
    phone = _e164(raw)
    if not phone:
        await message.answer("❌ Не похоже на номер. Введи в формате +79991234567:")
        return
    await _connect_send_code(message, state, phone)


@dp.message(Connect.code)
async def conn_got_code(message: Message, state: FSMContext):
    code = re.sub(r"\D", "", message.text or "")
    try:
        await message.delete()
    except Exception:
        pass
    p = _pending.get(message.chat.id)
    if not p:
        await state.clear()
        await message.answer("Сессия истекла. Повтори /connect.")
        return
    if not code:
        await message.answer("Введи код цифрами:")
        return
    client = p["client"]
    try:
        await client.sign_in(p["phone"], code, phone_code_hash=p["hash"])
    except SessionPasswordNeededError:
        await state.set_state(Connect.password)  # клиент уже в _pending
        await message.answer(
            "🔐 На аккаунте включён облачный пароль (2FA). Пришли его одним "
            "сообщением — удалю сразу после ввода."
        )
        return
    except PhoneCodeInvalidError:
        await message.answer("❌ Неверный код. Попробуй ввести ещё раз:")
        return
    except PhoneCodeExpiredError:
        _pending.pop(message.chat.id, None)
        await state.clear()
        await client.disconnect()
        await message.answer(
            "⌛ Код аннулирован (Telegram гасит коды, введённые цифрами подряд).\n"
            "Повтори /connect и в этот раз вводи код <b>через пробелы/дефисы</b>: "
            "напр. <b>1 2 3 4 5</b>.", parse_mode="HTML",
        )
        return
    except Exception as e:
        _pending.pop(message.chat.id, None)
        await state.clear()
        await client.disconnect()
        await message.answer(f"❌ Не удалось войти ({type(e).__name__}). Повтори /connect.")
        return
    _pending.pop(message.chat.id, None)
    await state.clear()
    await _finish(message, client)


async def _connect_qr(message: Message, state: FSMContext):
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
            _pending[message.chat.id] = {"client": client}
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
    account = _account_by("tg_user_id", message.from_user.id)
    if account:  # уже привязан -> персональное меню без «Привязать»
        name = pgconn.get_setting("user.full_name", None, account=account) or account
        await message.answer(START_LINKED.format(name=name),
                             reply_markup=_kb_linked(), parse_mode="HTML")
    else:
        await message.answer(START_TEXT, parse_mode="HTML")
        await _send_link_prompt(message)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT, parse_mode="HTML")


def _connected_account(user_id):
    """Аккаунт, у которого уже есть Telethon-сессия для этого TG-пользователя."""
    acc = _account_by("tg_user_id", user_id)
    if acc and (pgconn.app_config(account=acc).get("tg_user_session")):
        return acc
    return None


async def _connect_entry(message: Message, state: FSMContext, user_id: int):
    acc = _connected_account(user_id)
    if acc:
        name = pgconn.get_setting("user.full_name", None, account=acc) or acc
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="🔄 Переподключить", callback_data="conn:reconnect")]])
        await message.answer(
            f"✅ Telegram уже подключён для ГигаРекрутера (аккаунт «{name}»).\n"
            "Если сессия слетела — жми «Переподключить».", reply_markup=kb)
        return
    await start_connect(message, state)


@dp.callback_query(F.data == "conn:reconnect")
async def cb_conn_reconnect(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await start_connect(cq.message, state)


@dp.message(Command("connect"))
async def cmd_connect(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return
    await _connect_entry(message, state, message.from_user.id)


# ── лёгкая привязка для Mini App (по номеру телефона, без Telethon-сессии) ──
# Telethon /connect (полный доступ к TG) нужен ТОЛЬКО для ГигаРекрутера. Чтобы
# открыть профиль/статистику, достаточно сопоставить TG↔hh по телефону.

async def _send_link_prompt(message: Message):
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await message.answer(
        "Чтобы открыть личный кабинет, свяжем твой Telegram с hh-аккаунтом по "
        "номеру телефона. Нажми кнопку ниже 👇\n\n"
        "(нужен тот же номер, что указан на hh)", reply_markup=kb,
    )


def _already_linked_text(user_id):
    acc = _account_by("tg_user_id", user_id)
    if not acc:
        return None
    name = pgconn.get_setting("user.full_name", None, account=acc) or acc
    return (f"✅ Профиль уже привязан к «{name}». Открывай кабинет кнопкой "
            "«📊 Профиль» (слева от поля ввода) или /start.")


def _create_profile(user, phone: str) -> str:
    """Создать профиль (bare-аккаунт по TG-id) без hh: имя/телефон + общие конфиги
    (бот для уведомлений, vLLM для писем GetMatch). Аккаунты привяжутся через /addaccount."""
    account = str(user.id)
    full_name = (" ".join(x for x in [user.last_name, user.first_name] if x)
                 or user.username or "кандидат")  # не показываем числовой TG-id как имя
    pgconn.register_user(full_name, account)
    pgconn.set_app_config("tg_user_id", user.id, account=account)
    pgconn.set_app_config("hh_phone", phone, account=account)
    pgconn.set_setting("user.full_name", full_name, account=account)
    base = pgconn.app_config()
    if base.get("telegram"):
        pgconn.set_app_config("telegram", base["telegram"], account=account)
    if base.get("openai"):
        pgconn.set_app_config("openai", base["openai"], account=account)
    return full_name


@dp.message(F.contact)
async def on_contact(message: Message):
    if message.chat.type != "private":
        return
    c = message.contact
    # только свой контакт (защита от пересланного чужого)
    if c.user_id and c.user_id != message.from_user.id:
        await message.answer("Это чужой контакт. Поделись СВОИМ номером.",
                             reply_markup=ReplyKeyboardRemove())
        return
    existing = _account_by("tg_user_id", message.from_user.id)
    if existing:  # профиль уже есть — просто подтверждаем
        name = pgconn.get_setting("user.full_name", None, account=existing) or existing
        await message.answer(
            f"✅ Профиль «{name}» уже привязан. Открывай кабинет «📊 Профиль» "
            "или привяжи ещё аккаунт через /addaccount.",
            reply_markup=ReplyKeyboardRemove())
        return
    account = _account_by("hh_phone", c.phone_number)
    if account:  # есть hh-аккаунт с этим номером — линкуем к нему (как раньше)
        pgconn.set_app_config("tg_user_id", message.from_user.id, account=account)
        await message.answer(
            "✅ Привязано! Открывай профиль кнопкой «📊 Профиль» (слева от поля ввода).",
            reply_markup=ReplyKeyboardRemove())
        return
    # нет hh — создаём профиль; аккаунты (hh, GetMatch) привяжешь через /addaccount
    full_name = _create_profile(message.from_user, c.phone_number)
    await message.answer(
        f"✅ Профиль создан, <b>{full_name}</b>!\n\n"
        "Теперь привяжи аккаунты для поиска работы:\n"
        "• /addaccount → hh.ru (логин + пароль)\n"
        "• /addaccount → GetMatch (код из @g_jobbot)\n\n"
        "Или открой кабинет кнопкой «📊 Профиль».",
        reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")


@dp.message(Connect.password)
async def got_password(message: Message, state: FSMContext):
    pw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    p = _pending.pop(message.chat.id, None)
    client = p and p.get("client")
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


ALREADY_LINKED = (
    "У тебя уже привязан hh-аккаунт «{name}». Один Telegram — один аккаунт.\n"
    "Открой кабинет кнопкой «📊 Профиль» или /start."
)


async def _start_addaccount(reply: Message, user_id: int, state: FSMContext) -> bool:
    """Привязка hh к профилю. К bare-профилю прикрепляем hh; если hh уже есть — отказ;
    без профиля — просим /start. False = не начали."""
    linked = _account_by("tg_user_id", user_id)
    if not linked:
        await reply.answer("Сначала создай профиль: /start → поделись номером.")
        return False
    if (pgconn.app_config(account=linked) or {}).get("token"):
        name = pgconn.get_setting("user.full_name", None, account=linked) or linked
        await reply.answer(
            f"У профиля «{name}» уже привязан hh-аккаунт. Открой кабинет «📊 Профиль».")
        return False
    await _drop_login(reply.chat.id)
    await state.clear()
    await state.set_state(AddAcc.login)
    await state.update_data(attach_account=linked)
    await reply.answer(
        "➕ Привязка hh-аккаунта к профилю.\n"
        "Логин hh — email или телефон (напр. +79991234567):")
    return True


def _addaccount_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟦 hh.ru — логин + пароль", callback_data="addacc:hh")],
        [InlineKeyboardButton(text="🟩 GetMatch — код из @g_jobbot", callback_data="addacc:gm")],
    ])


async def _addaccount_menu(reply: Message, user_id: int):
    if not _account_by("tg_user_id", user_id):
        await reply.answer("Сначала создай профиль: /start → поделись номером.")
        return
    await reply.answer("➕ Что привязать к профилю?", reply_markup=_addaccount_kb())


async def _start_getmatch(reply: Message, user, state: FSMContext):
    """GetMatch: запросить код (username из Telegram) → попросить код из @g_jobbot."""
    account = _account_by("tg_user_id", user.id)
    if not account:
        await reply.answer("Сначала создай профиль: /start → поделись номером.")
        return
    if pgconn.get_setting("getmatch.session", None, account=account):
        await reply.answer("GetMatch уже привязан. Управляй в кабинете «📊 Профиль».")
        return
    username = (user.username or "").lstrip("@")
    if not username:
        await reply.answer("У твоего Telegram нет username — задай его в настройках Telegram "
                           "(это логин для GetMatch), потом повтори /addaccount.")
        return
    from getmatch_api import request_otp, GetMatchError
    await reply.answer("⏳ Запрашиваю код входа GetMatch…")
    try:
        res = await request_otp(username)
    except GetMatchError as e:
        await reply.answer(f"❌ Не удалось запросить код: {e}\n"
                           "Проверь, что у тебя есть аккаунт кандидата на GetMatch.")
        return
    if not res.get("sent_tg"):
        await reply.answer(
            "❌ GetMatch не отправил код в Telegram для @" + username + ". "
            "Похоже, у этого username нет аккаунта кандидата на GetMatch с подключённым "
            "Telegram. Сначала зарегистрируйся: открой @g_jobbot, пройди короткую регистрацию "
            "(специальность, зарплата), потом снова /addaccount → GetMatch.")
        return
    await state.clear()
    await state.set_state(GmLink.code)
    await state.update_data(gm_user=username, gm_account=account)
    await reply.answer(
        "📩 GetMatch прислал <b>код для входа</b> в бот @g_jobbot.\n"
        "Открой @g_jobbot, скопируй код и пришли его сюда:", parse_mode="HTML")


@dp.message(GmLink.code)
async def gm_code(message: Message, state: FSMContext):
    code = (message.text or "").strip()
    d = await state.get_data()
    if not d.get("gm_account") or not d.get("gm_user"):  # состояние слетело (рестарт бота)
        await state.clear()
        await message.answer("Сессия привязки истекла. Повтори: /addaccount → GetMatch.")
        return
    if not code.isdigit() or not (4 <= len(code) <= 6):
        await message.answer("Код — это 4-6 цифр из бота @g_jobbot. Пришли его ещё раз:")
        return  # остаёмся в GmLink.code
    from getmatch_api import authorize_with_code, GetMatchError
    await message.answer("⏳ Привязываю GetMatch…")
    try:
        me = await authorize_with_code(d["gm_account"], d["gm_user"], code)
    except GetMatchError as e:
        await message.answer(f"❌ {e}\nПришли код из @g_jobbot ещё раз или начни заново: /addaccount → GetMatch.")
        return  # состояние не чистим — можно переввести код
    await state.clear()
    pgconn.set_setting("feat.getmatch", True, account=d["gm_account"])
    name = ((me.get("first_name") or "") + " " + (me.get("last_name") or "")).strip()
    await message.answer(
        f"✅ GetMatch привязан{(' (' + name + ')') if name else ''}! "
        "Авто-отклики через GetMatch включены — управляй в кабинете «📊 Профиль».")


@dp.message(Command("addaccount"))
async def cmd_addaccount(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return
    await _addaccount_menu(message, message.from_user.id)


@dp.callback_query(F.data == "addacc:hh")
async def cb_addacc_hh(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await _start_addaccount(cq.message, cq.from_user.id, state)


@dp.callback_query(F.data == "addacc:gm")
async def cb_addacc_gm(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await _start_getmatch(cq.message, cq.from_user, state)


@dp.message(AddAcc.login)
async def acc_login(message: Message, state: FSMContext):
    login = (message.text or "").strip()
    kind = _valid_login(login)
    if not kind:
        await message.answer(
            "❌ Это не похоже на email или телефон. Введи корректный логин hh "
            "(напр. name@mail.ru или +79991234567):"
        )
        return  # остаёмся на шаге логина
    medium = "email" if kind == "email" else "phone"
    await state.update_data(login=login, medium=medium, mode="password", password="")
    await state.set_state(AddAcc.password)
    await message.answer("Пароль hh (удалю сообщение сразу):")


@dp.message(AddAcc.password)
async def acc_password(message: Message, state: FSMContext):
    import onboard
    pw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    if len(pw) < 4:
        await message.answer("❌ Пароль слишком короткий. Введи пароль hh:")
        return  # остаёмся на шаге пароля
    d = await state.get_data()
    medium = d.get("medium", "email")
    await state.update_data(password=pw)
    await message.answer("⏳ Авторизую hh через браузер… ~минуту, подожди.")
    sess = onboard.LoginSession()
    _login_sessions[message.chat.id] = sess
    try:
        st = await sess.start(d["login"], pw, medium, "password")
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
    digits = re.sub(r"\D", "", salary)
    if not digits or len(digits) < 4:
        await message.answer("❌ Зарплата должна быть числом, напр. 250000:")
        return  # остаёмся на шаге зарплаты (сессия жива)
    salary = digits
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
    # к bare-профилю (создан на /start) прикрепляем hh под его ключом — не плодим аккаунт
    account = d.get("attach_account") or re.sub(r"\W", "", acc_id)
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
        f"✅ hh-аккаунт {full_name} привязан к профилю — работает и API, и браузер.\n"
        f"Резюме: {pub[0].get('title','')}. Отклики пойдут по расписанию.\n\n"
        "Открывай кабинет кнопкой «📊 Профиль»."
    )


@dp.callback_query(F.data == "addacc")
async def cb_addacc(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await _addaccount_menu(cq.message, cq.from_user.id)


@dp.callback_query(F.data == "connect")
async def cb_connect(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await _connect_entry(cq.message, state, cq.from_user.id)


@dp.callback_query(F.data == "help")
async def cb_help(cq: CallbackQuery):
    await cq.answer()
    await cq.message.answer(HELP_TEXT, parse_mode="HTML")


async def main():
    token = (pgconn.app_config().get("telegram") or {}).get("token")
    if not token:
        print("tg_connect_bot: нет telegram-токена")
        return
    bot = Bot(token)
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть кабинет / привязать профиль"),
        BotCommand(command="addaccount", description="Привязать аккаунт (hh / GetMatch)"),
        BotCommand(command="connect", description="Дать доступ к Telegram (для авто-функций)"),
        BotCommand(command="help", description="Помощь"),
    ])
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="📊 Профиль", web_app=WebAppInfo(url=WEBAPP_URL)
            )
        )
    except Exception as e:
        print("set_chat_menu_button:", repr(e)[:80])
    print("tg_connect_bot (aiogram): меню установлено, слушаю команды…")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
