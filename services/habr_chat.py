"""Habr Career — авто-ответ в чате работодателям (как hh reply_employers).

Читает диалоги (`/api/frontend_v1/chat/conversations`); там, где ПОСЛЕДНЕЕ сообщение от
собеседника (isMine=false), свежее и от работодателя (не Хабр-стафф) — LLM решает, отвечать
ли (реальное предложение/вопрос -> ответ; рассылка/спам -> SKIP) и пишет ответ.
Гейт: feat.habr + habr.session. Запуск: python /app/services/habr_chat.py [--dry].
"""
import asyncio
import datetime
import random
import re
import sys

import habr_api
from hh_applicant_tool.ai import ChatOpenAI
from hh_applicant_tool.storage import pgconn

DRY = "--dry" in sys.argv
MAX_REPLIES = 10     # ответов за прогон
MAX_AGE_DAYS = 30    # не отвечаем на сообщения старше N дней (стейл — кандидат уже мимо)

REPLY_SYS = (
    "Ты — кандидат на работу, отвечаешь в личном чате на Хабр Карьере. Тебе пишет "
    "рекрутёр/работодатель. Если это реальное предложение работы или вопрос по вакансии — "
    "ответь живо и по-человечески, кратко (2-4 предложения), строго по опыту ниже: вырази "
    "интерес, дай 1-2 релевантных факта, предложи созвон/обсудить детали. Если сообщение НЕ от "
    "работодателя (рассылка платформы, новости, спам, не про конкретную работу) ИЛИ отвечать "
    "по сути не на что — верни РОВНО одно слово: SKIP. Без markdown, без слова «резюме», не "
    "выдумывай навыков, которых нет в опыте.\n\n=== ОПЫТ ===\n{resume}\n=== КОНЕЦ ОПЫТА ===")


def _strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "").replace("&nbsp;", " ")).strip()


def _too_old(created_at):
    try:
        dt = datetime.datetime.strptime((created_at or "")[:10], "%Y-%m-%d")
        return (datetime.datetime.now() - dt).days > MAX_AGE_DAYS
    except Exception:
        return False


async def _reply(oa, resume, convo_text):
    if not (oa and oa.get("token") and resume):
        return ""
    try:
        chat = ChatOpenAI(token=oa["token"], model=oa.get("model"),
                          completion_endpoint=oa.get("completion_endpoint"),
                          system_prompt=REPLY_SYS.format(resume=resume[:3000]),
                          temperature=0.5, max_completion_tokens=320)
        t = ((await chat.send_message(convo_text)) or "").strip()
    except Exception as e:
        print(f"habr-chat: LLM не ответил ({type(e).__name__})")
        return ""
    if t.upper().startswith("SKIP") or len(t) < 15:
        return ""
    return t


async def run():
    account = pgconn.get_account()
    if not pgconn.feature_enabled("habr"):
        print("habr-chat: feat выключен — пропуск")
        return
    if not pgconn.get_setting("habr.session", account=account):
        print("habr-chat: нет сессии — пропуск")
        return

    lock_conn = pgconn.connect()
    api = None
    locked = False
    try:
        with lock_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (f"habr-chat:{account}",))
            if not cur.fetchone()[0]:
                print("habr-chat: уже выполняется — пропуск")
                return
        locked = True

        cfg = pgconn.app_config()
        oa = cfg.get("openai")
        resume = (cfg.get("resume_text") or "").strip()
        api = habr_api.HabrAPI(account)
        try:
            await api.ensure_auth()
        except habr_api.HabrError as e:
            print(f"habr-chat: логин не удался: {e}")
            if not DRY:
                raise
            return

        convs = await api.conversations()
        print(f"habr-chat: диалогов {len(convs)}")
        seen = pgconn.seen_keys("habr_chat")
        replied = 0
        for c in convs:
            if replied >= MAX_REPLIES:
                break
            if c.get("careerStaff") or c.get("banned"):
                continue  # стафф Хабр Карьеры / забанен — не работодатель
            login = c.get("login")
            if not login:
                continue
            msgs = await api.messages(login)
            if not msgs:
                continue
            last = msgs[-1]
            if last.get("isMine"):
                continue  # последнее — наше, уже ответили
            key = f"{login}:{last.get('id')}"
            if key in seen:
                continue
            if _too_old(last.get("createdAt", "")):
                continue  # старое — не реанимируем
            company = c.get("companyName") or ""
            convo = "\n".join(
                f"{'Я' if m.get('isMine') else (c.get('fullName') or 'Собеседник')}: {_strip_html(m.get('body'))}"
                for m in msgs[-6:])
            reply = await _reply(oa, resume, convo)
            if not reply:
                print(f"habr-chat: {c.get('fullName')} ({company}) — LLM решил не отвечать (SKIP)")
                pgconn.add_seen("habr_chat", key)  # больше не дёргаем это сообщение
                continue
            if DRY:
                print(f"habr-chat[dry]: ответил бы {c.get('fullName')} ({company}): {reply[:90]}")
                replied += 1
                continue
            r = await api.send_message(login, reply)
            if r.status_code in (200, 201):
                pgconn.add_seen("habr_chat", key)
                pgconn.bump_activity("habr_chat", 1, account=account)
                replied += 1
                print(f"habr-chat: ответил {c.get('fullName')} ({company})")
            else:
                print(f"habr-chat: не отправилось {login} ({r.status_code}) — повторю позже")
            await asyncio.sleep(random.uniform(3, 8))
        print(f"habr-chat: готово, ответов {replied}")
    finally:
        if api is not None:
            await api.aclose()
        if locked:
            with lock_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"habr-chat:{account}",))
            lock_conn.commit()
        lock_conn.close()


if __name__ == "__main__":
    asyncio.run(run())
