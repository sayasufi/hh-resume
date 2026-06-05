"""Авто-отклик на ленту GetMatch через Telegram-бота @g_jobbot (Telethon).

Лента = история push-сообщений бота: каждая вакансия — сообщение с inline-кнопкой
«💥 Откликнуться в боте» (callback, данные `application__send__{vacancy_id}`). Идём по
истории чата, кликаем callback на НОВЫХ вакансиях (дедуп `seen_keys('getmatch')`),
дневной лимит. Кнопки «Смотреть/Вакансии (N)/Еще N» — мини-апп, не нужны.

Гейт: feat.getmatch + app_config.tg_user_session. Профиль ОБЯЗАН быть подтверждён —
иначе не откликаемся, кладём дело «подтвердите профиль». Отправленные отклики пишем в
`getmatch_apps` (реестр: название/ссылка/дата) — для просмотра в кабинете.

Запуск: python /app/services/getmatch_apply.py [--dry]   (обычно через Prefect JOBS).
"""
import asyncio
import random
import re
import sys

from hh_applicant_tool.storage import pgconn

DRY = "--dry" in sys.argv
BOT = "g_jobbot"
SEEN_KIND = "getmatch"
DEFAULT_MAX = 50

_SEND_RE = re.compile(rb"application__send__(\d+)")
_APPLIED_RE = re.compile(r"отклик.{0,20}отправлен", re.I)
_EXPIRED_RE = re.compile(r"неактуальн|закрыт|больше не|снят\w* с публик", re.I)


# ── чистые хелперы (тестируемые) ─────────────────────────────────────────────
def apply_callback_id(button):
    """id вакансии из callback-данных кнопки отклика (application__send__{id})."""
    data = getattr(button, "data", None)
    if not data:
        return None
    m = _SEND_RE.search(data)
    return m.group(1).decode() if m else None


def find_apply_button(buttons):
    """Кнопка отклика: сперва callback application__send__…, иначе матч по тексту."""
    for row in (buttons or []):
        for b in row:
            if apply_callback_id(b):
                return b
    for row in (buttons or []):
        for b in row:
            t = getattr(b, "text", "") or ""
            if "Откликнуться" in t or "💥" in t:
                return b
    return None


def is_profile_ok(text: str) -> bool:
    """Профиль подтверждён? (маркеры из /profile)."""
    t = (text or "").lower()
    return "подтвержд" in t or "в один клик" in t


def applied_ok(text: str) -> bool:
    """Ответ бота = НОВЫЙ отклик отправлен?"""
    t = text or ""
    return bool(_APPLIED_RE.search(t)) or "Мои отклики" in t


def is_already(text: str) -> bool:
    """Ответ бота = на эту вакансию уже откликались ранее."""
    return "уже откликал" in (text or "").lower()


def is_expired(text: str) -> bool:
    """Ответ бота = вакансия неактуальна/закрыта?"""
    return bool(_EXPIRED_RE.search(text or ""))


def first_line(text: str) -> str:
    """Первая строка (название вакансии) из текста сообщения."""
    lines = (text or "").strip().splitlines()
    return (lines[0] if lines else "")[:200]


# ── операция ─────────────────────────────────────────────────────────────────
def _today_count(account: str) -> int:
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(count,0) FROM activity_daily "
                        "WHERE account=%s AND kind=%s AND day=current_date",
                        (account, SEEN_KIND))
            r = cur.fetchone()
        return int(r[0]) if r else 0
    finally:
        conn.close()


def _record_app(account, vid, title, url):
    """Записать отправленный отклик в реестр getmatch_apps (для кабинета)."""
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS getmatch_apps ("
                        "account text NOT NULL, vacancy_id text NOT NULL, title text, url text, "
                        "applied_at timestamptz DEFAULT now(), PRIMARY KEY (account, vacancy_id))")
            cur.execute("INSERT INTO getmatch_apps(account, vacancy_id, title, url) "
                        "VALUES (%s,%s,%s,%s) ON CONFLICT(account, vacancy_id) DO NOTHING",
                        (account, vid, title, url))
        conn.commit()
    finally:
        conn.close()


async def _read_new(client, ent, after_id):
    msgs = await client.get_messages(ent, limit=10)
    return [m for m in msgs if m.id > after_id and not m.out]


async def run():
    account = pgconn.get_account()
    if not pgconn.feature_enabled("getmatch"):
        print("getmatch: feat.getmatch выключен — пропуск"); return
    cfg = pgconn.app_config()
    enc = cfg.get("tg_user_session")
    if not enc:
        print("getmatch: нет tg_user_session — пропуск"); return

    lock_conn = pgconn.connect()
    with lock_conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (f"getmatch:{account}",))
        if not cur.fetchone()[0]:
            print("getmatch: уже выполняется для аккаунта — пропуск")
            lock_conn.close(); return
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        api_id, api_hash = pgconn.tg_api()
        client = TelegramClient(StringSession(pgconn.dec_session(enc)), api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            print("getmatch: сессия не авторизована — нужен /connect"); return
        ent = await client.get_entity(BOT)

        # --- profile-гейт (обязательно) ---
        before = (await client.get_messages(ent, limit=1))[0].id
        await client.send_message(ent, "/profile")
        await asyncio.sleep(5)
        prof = " ".join((m.text or "") for m in await _read_new(client, ent, before))
        if not is_profile_ok(prof):
            print("getmatch: профиль НЕ подтверждён — не откликаемся, кладём дело")
            if not DRY:
                pgconn.notify(pgconn.PRIORITY_MED,
                              "Подтвердите профиль на GetMatch (@g_jobbot, команда /profile), "
                              "чтобы бот начал откликаться за вас.",
                              category="action", link="https://t.me/g_jobbot",
                              dedup_key="getmatch:profile")
            return
        print("getmatch: профиль подтверждён")

        limit = int(pgconn.get_setting("getmatch.max_per_day", DEFAULT_MAX) or DEFAULT_MAX)
        sent_today = _today_count(account)
        if sent_today >= limit:
            print(f"getmatch: дневной лимит достигнут ({sent_today}/{limit})"); return
        seen = pgconn.seen_keys(SEEN_KIND)

        # --- проход по истории чата: новые вакансии -> клик callback ---
        msgs = await client.get_messages(ent, limit=400)  # newest-first
        applied = 0
        for m in msgs:
            if sent_today + applied >= limit:
                break
            ab = find_apply_button(m.buttons)
            if not ab:
                continue
            vid = apply_callback_id(ab)
            if not vid or vid in seen:
                continue
            if DRY:
                print(f"getmatch[dry]: откликнулся бы на vac {vid}")
                seen.add(vid)  # только в памяти — dry НЕ помечает seen в БД
                applied += 1
                continue
            before_click = (await client.get_messages(ent, limit=1))[0].id
            alert = ""
            try:
                r = await ab.click()
                alert = getattr(r, "message", "") or ""
            except Exception as e:
                print(f"getmatch: клик vac {vid} не удался: {repr(e)[:80]}")
                pgconn.add_seen(SEEN_KIND, [vid]); seen.add(vid)
                continue
            await asyncio.sleep(3)
            newtxt = " ".join((mm.text or "") for mm in await _read_new(client, ent, before_click))
            resp = (alert + " " + newtxt).strip()
            pgconn.add_seen(SEEN_KIND, [vid]); seen.add(vid)
            if is_already(resp):
                print(f"getmatch: vac {vid} — уже откликались ранее, seen")
            elif applied_ok(resp):
                pgconn.bump_activity("getmatch", 1)
                _record_app(account, vid, first_line(getattr(m, "raw_text", None) or m.text),
                            f"https://getmatch.ru/vacancies/{vid}")
                applied += 1
                print(f"getmatch: отклик на vac {vid} ({sent_today + applied}/{limit})")
                await asyncio.sleep(random.uniform(5, 15))
            elif is_expired(resp):
                print(f"getmatch: vac {vid} неактуальна — пропуск")
            else:
                print(f"getmatch: vac {vid} — неясный ответ ({resp[:60]!r}), помечен seen")
        print(f"getmatch: готово, откликов за прогон: {applied} "
              f"(сегодня {sent_today + applied}/{limit})")
        await client.disconnect()
    finally:
        with lock_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"getmatch:{account}",))
        lock_conn.close()


if __name__ == "__main__":
    asyncio.run(run())
