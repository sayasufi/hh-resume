"""Источник: Telegram-каналы с вакансиями.

Читает настроенные каналы (userbot-сессия кандидата). По каждому новому посту LLM решает:
одиночная ли это вакансия под профиль кандидата (не дайджест/реклама) и есть ли в посте контакт
для отклика (@username рекрутёра). Если да — пишем рекрутёру персональное ЛС + ссылку на резюме.
Если в посте бот-ссылка для отклика — заводим «Дело» (его добьёт auto_screen, он source-agnostic).
Иначе — скип. Дедуп по `канал:post_id` (seen_keys 'tg_channels'), жёсткий rate-limit (холодные ЛС).

DRY по умолчанию (как auto_screen) — реально пишет только с --live.
Гейт: feat.tg_channels + tg_user_session. Запуск: python /app/services/tg_channels.py [--live]
"""
import asyncio
import random
import re
import sys
from datetime import datetime, timedelta, timezone

from hh_applicant_tool.ai import ChatOpenAI
from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn
from telethon import TelegramClient
from telethon.sessions import StringSession

LIVE = "--live" in sys.argv
DRY = not LIVE
MAX_DM = 6              # холодных ЛС за прогон — spam-safety (Telegram флагает рассылку незнакомцам)
MAX_EVAL = 60          # потолок LLM-оценок постов за прогон (стоимость)
POSTS_PER_CH = 12      # сколько свежих постов смотреть на канал
FRESH_DAYS = 3         # посты старше — не трогаем
TME = re.compile(r"t\.me/([A-Za-z0-9_]{4,32})")

SYS = (
    "Тебе дают пост из Telegram-канала с IT-вакансиями. Оцени его относительно опыта кандидата ниже.\n"
    "Верни СТРОГО три строки:\n"
    "MATCH: да — если это ОДНА конкретная вакансия, подходящая кандидату по стеку/уровню; "
    "нет — если это дайджест из многих вакансий, реклама/курс/инфопродукт, не про конкретную работу, "
    "или вакансия не по профилю.\n"
    "CONTACT: @username — если в посте есть прямой Telegram-контакт рекрутёра/нанимающего для отклика "
    "(«пишите @...», «резюме @...»); иначе НЕТ. НЕ бери @каналы/@ботов и не выдумывай.\n"
    "ПИСЬМО: <если MATCH=да и есть CONTACT — короткое персональное сообщение рекрутёру в одну строку: "
    "поздоровайся, скажи что заинтересовала вакансия (назови её), 1-2 релевантных факта строго из опыта "
    "ниже, готовность обсудить. Без markdown, без слова «резюме», не выдумывай навыки. Иначе: ->>\n\n"
    "=== ОПЫТ КАНДИДАТА ===\n{resume}\n=== КОНЕЦ ===")


def _strip(s):
    return re.sub(r"\s+", " ", s or "").strip()


async def _decide(oa, resume, post):
    """LLM -> (match: bool, contact: '@x'|'', letter: str)."""
    if not (oa and oa.get("token") and resume):
        return False, "", ""
    try:
        chat = ChatOpenAI(token=oa["token"], model=oa.get("model"),
                          completion_endpoint=oa.get("completion_endpoint"),
                          system_prompt=SYS.format(resume=resume[:3000]),
                          temperature=0.4, max_completion_tokens=320)
        t = ((await chat.send_message(post[:2500])) or "").strip()
    except Exception as e:
        print(f"  LLM err {type(e).__name__}")
        return False, "", ""
    match, contact, letter = False, "", ""
    for line in t.splitlines():
        s = line.strip()
        up = s.upper()
        if up.startswith("MATCH:"):
            match = "да" in s.lower()
        elif up.startswith("CONTACT:"):
            v = s.split(":", 1)[1].strip()
            m = re.search(r"@([A-Za-z0-9_]{4,32})", v)
            if m and not m.group(1).lower().endswith("bot"):
                contact = m.group(1)
        elif up.startswith("ПИСЬМО:"):
            v = s.split(":", 1)[1].strip()
            if v and len(v) >= 15 and "->" not in v:
                letter = v
    return match, contact, letter


def _channels(account):
    raw = (pgconn.get_setting("tg.channels", account=account)
           or pgconn.get_setting("tg.channels_default", account="_global") or "")
    return [c.strip().lstrip("@") for c in raw.split(",") if c.strip()]


async def _hh_resume_url(cfg, account):
    rid = pgconn.get_setting("apply.resume_id", account=account)
    tok = (cfg.get("token") or {})
    if not (rid and tok.get("access_token")):
        return ""
    api = ApiClient(access_token=tok["access_token"], refresh_token=tok.get("refresh_token"),
                    access_expires_at=tok.get("access_expires_at"),
                    user_agent=generate_android_useragent(), refresh_hook=pgconn.locked_token_refresh)
    try:
        r = await api.get(f"/resumes/{rid}")
        return r.get("alternate_url") or f"https://hh.ru/resume/{rid}"
    except Exception:
        return f"https://hh.ru/resume/{rid}"
    finally:
        await api.aclose()


async def run():
    account = pgconn.get_account()
    if not pgconn.feature_enabled("tg_channels"):
        print("tg_channels: feat выключен — пропуск")
        return
    cfg = pgconn.app_config()
    enc = cfg.get("tg_user_session")
    oa = cfg.get("openai") or {}
    if not enc or not oa.get("token"):
        print("tg_channels: нет tg-сессии / openai — пропуск")
        return
    resume = (cfg.get("resume_text") or "").strip()
    if not resume:
        print("tg_channels: нет resume_text — пропуск (матчинг будет мусорным)")
        return
    channels = _channels(account)
    if not channels:
        print("tg_channels: список каналов пуст — пропуск")
        return

    hh_url = await _hh_resume_url(cfg, account)
    api_id, api_hash = pgconn.tg_api()
    client = TelegramClient(StringSession(pgconn.dec_session(enc)), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("tg_channels: tg-сессия слетела — пропуск")
        return
    seen = pgconn.seen_keys("tg_channels")
    cutoff = datetime.now(timezone.utc) - timedelta(days=FRESH_DAYS)
    dm = evals = deals = 0
    print(f"tg_channels[{account}] режим={'LIVE' if LIVE else 'DRY'}: каналов {len(channels)}")
    try:
        for ch in channels:
            if dm >= MAX_DM or evals >= MAX_EVAL:
                break
            try:
                msgs = await client.get_messages(ch, limit=POSTS_PER_CH)
            except Exception as e:
                print(f"  @{ch}: не прочитать ({type(e).__name__})")
                continue
            for m in msgs:
                if dm >= MAX_DM or evals >= MAX_EVAL:
                    break
                if not m.message or (m.date and m.date < cutoff):
                    continue
                key = f"{ch}:{m.id}"
                if key in seen:
                    continue
                post = _strip(m.message)
                evals += 1
                match, contact, letter = await _decide(oa, resume, post)
                pgconn.add_seen("tg_channels", key)
                seen.add(key)
                if not match:
                    continue
                bot = next((u for u in TME.findall(m.message)
                            if u.lower().endswith("bot") and "giga" not in u.lower()), None)
                if contact and letter:
                    body = letter + (f"\nМоё резюме: {hh_url}" if hh_url else "")
                    if DRY:
                        print(f"  [ЛС] @{contact} (из @{ch}): {letter[:80]}")
                        dm += 1
                        continue
                    try:
                        ent = await client.get_entity(contact)
                        await client.send_message(ent, body, link_preview=False)
                        pgconn.bump_activity("tg_channels", 1, account=account)
                        dm += 1
                        print(f"  [ЛС] написал @{contact} (из @{ch})")
                    except Exception as e:
                        print(f"  [ЛС] @{contact}: не отправилось ({type(e).__name__})")
                    await asyncio.sleep(random.uniform(5, 14))
                elif bot:
                    if not DRY:
                        pgconn.add_action_items([{
                            "nid": None, "chat_id": None,
                            "vacancy": f"Вакансия из @{ch}",
                            "action": f"пройти анкету в Telegram-боте t.me/{bot}",
                            "chat_url": f"https://t.me/{ch}", "vacancy_url": "",
                            "action_url": f"https://t.me/{bot}"}])
                    deals += 1
                    print(f"  [ДЕЛО] бот t.me/{bot} (из @{ch})")
    finally:
        await client.disconnect()
    print(f"tg_channels: готово — ЛС {dm}, дел {deals}, оценено постов {evals}")


if __name__ == "__main__":
    asyncio.run(run())
