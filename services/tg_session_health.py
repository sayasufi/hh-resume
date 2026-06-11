"""Health-check Telegram-userbot-сессий кандидатов/краулера: жива ли (authorized+get_me),
не во флуде ли. Пишет _health.tg_session (account) -> кабинет «Источники» + дневной алерт.
Запуск: python /app/services/tg_session_health.py  (через Prefect JOBS, feature=None)."""
import asyncio, sys, time
sys.path.insert(0, "/app")
from hh_applicant_tool.storage import pgconn
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError


async def _probe(enc):
    c = TelegramClient(StringSession(pgconn.dec_session(enc)), *pgconn.tg_api())
    try:
        await c.connect()
        if not await c.is_user_authorized():
            return False, "сессия слетела — нужен повторный /connect"
        me = await c.get_me()
        uname = ("@" + me.username) if (me and me.username) else (f"id{me.id}" if me else "?")
        return True, f"жива ({uname})"
    except FloodWaitError as e:
        return False, f"флуд-вейт ~{e.seconds // 60} мин"
    except Exception as e:
        return False, f"ошибка подключения ({type(e).__name__})"
    finally:
        try:
            await c.disconnect()
        except Exception:
            pass


async def main():
    # per-account: оркестратор (feature=None) запускает по каждому аккаунту через HH_ACCOUNT
    a = pgconn.get_account()
    cfg = pgconn.app_config(a)
    enc = cfg.get("tg_user_session")
    if not enc:
        print(f"{a}: нет TG-сессии — нечего проверять")
        return
    now = int(time.time())
    fu = pgconn.get_setting("tg_flood_until", account=a)
    try:
        fu = int(fu) if fu else 0
    except Exception:
        fu = 0
    if fu and now < fu:  # активный флуд-вейт (записан операциями) — не перетираем на «жива»
        pgconn.record_health("tg_session", False, f"флуд-вейт ещё ~{(fu - now) // 60} мин", account=a)
        print(f"{a}: ФЛУД ещё ~{(fu - now) // 60} мин")
        return
    ok, detail = await _probe(enc)
    pgconn.record_health("tg_session", ok, detail, account=a)
    print(f"{a}: {'OK' if ok else 'FAIL'} — {detail}")


if __name__ == "__main__":
    asyncio.run(main())
