"""Habr Career — авто-ответ в чате работодателям (как hh reply_employers).

Читает диалоги (`/api/frontend_v1/chat/conversations`); там, где ПОСЛЕДНЕЕ сообщение от
собеседника (isMine=false), свежее и от работодателя (не Хабр-стафф) — LLM решает: что
ответить (или SKIP) И нужно ли действие самого кандидата (-> заводим «Дело»). Бот отвечает,
где может; где нужен человек (назначить созвон, тестовое, оффер) — кладёт в дела.
Гейт: feat.habr_chat + habr.session. Запуск: python /app/services/habr_chat.py [--dry].
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
    "Ты — кандидат на работу, тебе пишет рекрутёр/работодатель в личном чате на Хабр Карьере. "
    "Проанализируй последнее сообщение собеседника и верни СТРОГО две строки:\n"
    "ОТВЕТ: <одной строкой — что написать рекрутёру: живо, кратко (2-4 предложения), по опыту "
    "ниже, вырази интерес и 1-2 релевантных факта, предложи обсудить. НЕ называй конкретное "
    "время/условия. Или SKIP — если это рассылка платформы/спам/не про конкретную работу>\n"
    "ДЕЛО: <что должен сделать САМ кандидат, а бот не может: назначить конкретное время созвона, "
    "выполнить тестовое задание, принять решение по офферу, прислать документы. Или НЕТ — если "
    "ответом всё закрыто>\n"
    "Не выдумывай навыков, которых нет в опыте. Без markdown, без слова «резюме».\n\n"
    "=== ОПЫТ ===\n{resume}\n=== КОНЕЦ ОПЫТА ===")


def _strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "").replace("&nbsp;", " ")).strip()


def _too_old(created_at):
    try:
        dt = datetime.datetime.strptime((created_at or "")[:10], "%Y-%m-%d")
        return (datetime.datetime.now() - dt).days > MAX_AGE_DAYS
    except Exception:
        return False


# мета-маркеры: LLM иногда выдаёт рассуждение/варианты вместо самого ответа — такое не шлём рекрутёру
_META = (
    "отвечать не требуется", "не требует ответа", "отвечать не нужно", "можно не отвечать",
    "можно написать", "можно ответить", "вы можете написать", "достаточно написать",
    "переписка завершена", "в данной ситуации", "в этой ситуации", "в качестве ассистента",
    "как ассистент", "я ассистент", "как ии", "я не могу", "следующий шаг",
)


def _is_meta(v):
    low = (v or "").lower()
    return any(p in low for p in _META) or len(v) > 600


async def _decide(oa, resume, convo_text):
    """LLM -> (reply, task). reply='' если SKIP; task='' если действие человека не нужно."""
    if not (oa and oa.get("token") and resume):
        return "", ""
    try:
        chat = ChatOpenAI(token=oa["token"], model=oa.get("model"),
                          completion_endpoint=oa.get("completion_endpoint"),
                          system_prompt=REPLY_SYS.format(resume=resume[:3000]),
                          temperature=0.5, max_completion_tokens=320)
        t = ((await chat.send_message(convo_text)) or "").strip()
    except Exception as e:
        print(f"habr-chat: LLM не ответил ({type(e).__name__})")
        return "", ""
    reply, task = "", ""
    for line in t.splitlines():
        s = line.strip()
        if s.upper().startswith("ОТВЕТ:"):
            v = s.split(":", 1)[1].strip().strip('"«». ')
            if v and not v.upper().startswith("SKIP") and len(v) >= 12 and not _is_meta(v):
                reply = v
        elif s.upper().startswith("ДЕЛО:"):
            v = s.split(":", 1)[1].strip()
            if v and v.strip(" .").upper() not in ("НЕТ", "-", "NO", "НЕ ТРЕБУЕТСЯ"):
                task = v
    return reply, task


async def run():
    account = pgconn.get_account()
    if not pgconn.feature_enabled("habr_chat"):
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
            reply, task = await _decide(oa, resume, convo)
            if not reply and not task:
                print(f"habr-chat: {c.get('fullName')} ({company}) — SKIP")
                pgconn.add_seen("habr_chat", key)  # больше не дёргаем это сообщение
                continue
            conv_url = f"https://career.habr.com/conversations/{login}"
            if DRY:
                bits = (f"ОТВЕТ: {reply[:70]}" if reply else "") + (f" | ДЕЛО: {task[:55]}" if task else "")
                print(f"habr-chat[dry]: {c.get('fullName')} ({company}) -> {bits}")
                pgconn.add_seen("habr_chat", key)
                replied += 1
                continue
            ok = True
            if reply:
                r = await api.send_message(login, reply)
                if r.status_code in (200, 201):
                    pgconn.bump_activity("habr_chat", 1, account=account)
                    print(f"habr-chat: ответил {c.get('fullName')} ({company})")
                else:
                    ok = False
                    print(f"habr-chat: не отправилось {login} ({r.status_code}) — повторю позже")
            if task and ok:  # нужно действие человека -> в «Дела»
                pgconn.add_action_items([{
                    "nid": None, "chat_id": None, "vacancy": company or c.get("fullName"),
                    "action": task, "chat_url": conv_url, "vacancy_url": "", "action_url": conv_url,
                }])
                print(f"habr-chat: дело для тебя — {task[:60]}")
            if ok:
                pgconn.add_seen("habr_chat", key)
                replied += 1
            await asyncio.sleep(random.uniform(3, 8))
        print(f"habr-chat: готово, обработано {replied}")
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
