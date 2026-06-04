#!/usr/bin/env python3
"""ГигаРекрутер-автопрохождение интервью (standalone, per-account через run_all).

1) ПОИСК: сканирует сообщения hh-переписок на ссылку-приглашение
   t.me/Giga_recruiter_bot?start=<token> (по вакансии свой токен) -> очередь giga_queue.
2) ПРОХОЖДЕНИЕ: по очереди (по одному) берёт pending-токен, шлёт боту /start <token>,
   ведёт диалог Q&A (ждёт вопрос -> LLM-ответ от лица кандидата -> отправляет) до
   завершения, помечает done, переходит к следующему.

Один чат @Giga_recruiter_bot на все интервью -> строго по очереди (advisory-lock).
Гейт: feature_enabled('giga') И app_config['tg_user_session']. Без копий в личку.

Запуск:  python giga_recruiter.py [--dry]   (обычно через run_all)
"""
import asyncio
import re
import sys
import time

from telethon import TelegramClient
from telethon.sessions import StringSession

from hh_applicant_tool.ai import ChatOpenAI
from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn

DRY = "--dry" in sys.argv

DEFAULT_BOT = "Giga_recruiter_bot"
SCAN_CAP = 120                # сколько НЕпросмотренных переписок сканировать за прогон
RUN_BUDGET_SEC = 240          # бюджет на прогон (несколько интервью за раз)
MAX_TURNS = 30                # потолок ходов на одно интервью
REPLY_TIMEOUT = 45            # сколько ждать ответ бота (сек)
POLL_EVERY = 3
MAX_ANSWER_CHARS = 1500

GIGA_LINK_RE = re.compile(r"Giga_recruiter_bot\?start=([A-Za-z0-9_\-]+)", re.I)
DONE_RE = re.compile(
    r"заверш|спасибо за|благодар|результат|переда[мдл]|до связи|всего доброго|"
    r"оцен(им|ка|ить)|итог|на этом всё|до встречи|обратн(ую|ой) связ", re.I)

SYS_TMPL = (
    "Ты — кандидат {name}, проходишь первичное интервью с автоматическим рекрутёром "
    "(ГигаРекрутер) в Telegram. Отвечай ОТ ПЕРВОГО ЛИЦА, кратко, уверенно и по делу "
    "на последний вопрос, опираясь СТРОГО на факты из своего резюме ниже. Не выдумывай "
    "опыт, которого нет в резюме. Без приветствий в каждом сообщении, не повторяйся. "
    "Если просят согласие/готовность — соглашайся. Пиши по-русски, без markdown.\n\n"
    "=== РЕЗЮМЕ ===\n{resume}\n=== КОНЕЦ РЕЗЮМЕ ===")


def _label() -> str:
    try:
        return pgconn.get_setting("user.full_name") or pgconn.get_account()
    except Exception:
        return pgconn.get_account()


def _lock(account):
    conn = pgconn.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (f"giga:{account}",))
        got = cur.fetchone()[0]
    return conn, got


def _unlock(conn, account):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"giga:{account}",))
    finally:
        conn.close()


def _queue_add(account, token, vacancy, nid):
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO giga_queue(account, token, vacancy, nid) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT(account, token) DO NOTHING",
                (account, token, vacancy, nid))
            added = cur.rowcount
        conn.commit()
        return added
    finally:
        conn.close()


def _next_pending(account):
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT token, vacancy FROM giga_queue WHERE account=%s AND "
                "status='pending' ORDER BY created_at LIMIT 1", (account,))
            return cur.fetchone()
    finally:
        conn.close()


def _set_status(account, token, status, turns=None):
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            if turns is None:
                cur.execute("UPDATE giga_queue SET status=%s, updated_at=now() "
                            "WHERE account=%s AND token=%s", (status, account, token))
            else:
                cur.execute("UPDATE giga_queue SET status=%s, turns=%s, updated_at=now() "
                            "WHERE account=%s AND token=%s",
                            (status, turns, account, token))
        conn.commit()
    finally:
        conn.close()


async def _discover(token, account, cap=SCAN_CAP):
    """Глубокий скан hh-переписок на giga-ссылки -> очередь. Дедуп просмотренных
    через seen_keys('giga_scanned') (помечаем переписку, где работодатель уже
    ответил или нашли токен — чтобы не пересканировать). -> кол-во новых токенов."""
    api = ApiClient(
        access_token=token["access_token"], refresh_token=token.get("refresh_token", ""),
        access_expires_at=token.get("access_expires_at", 0),
        user_agent=generate_android_useragent(),
        refresh_hook=pgconn.locked_token_refresh,
    )
    scanned = pgconn.seen_keys("giga_scanned")
    found, checked, mark = 0, 0, []
    try:
        page = 0
        while checked < cap and page < 12:
            neg = await api.get("/negotiations", per_page=100, page=page,
                                order_by="updated_at")
            items = neg.get("items", [])
            if not items:
                break
            for n in items:
                if checked >= cap:
                    break
                if ((n.get("state") or {}).get("id") or "") == "discard":
                    continue
                nid = n.get("id")
                if str(nid) in scanned:
                    continue
                checked += 1
                vac = (n.get("vacancy") or {}).get("name") or ""
                try:
                    msgs = (await api.get(f"/negotiations/{nid}/messages")).get("items", [])
                except Exception:
                    continue
                got = False
                for m in msgs:
                    for tok in GIGA_LINK_RE.findall(m.get("text") or ""):
                        found += _queue_add(account, tok, vac, nid)
                        got = True
                emp = any((m.get("author") or {}).get("participant_type") == "employer"
                          for m in msgs)
                if got or emp:  # переписка «осела» -> не сканируем повторно
                    mark.append(nid)
            if page + 1 >= neg.get("pages", 1):
                break
            page += 1
    except Exception as e:
        print(f"giga discover: {repr(e)[:140]}")
    finally:
        await api.aclose()
    if mark:
        pgconn.add_seen("giga_scanned", mark)
    return found


async def _wait_reply(client, entity, after_id, timeout=REPLY_TIMEOUT):
    """Ждать новые входящие сообщения бота (id > after_id). -> список (хронологически)."""
    waited = 0
    while waited < timeout:
        await asyncio.sleep(POLL_EVERY)
        waited += POLL_EVERY
        msgs = await client.get_messages(entity, limit=8)
        new = sorted([m for m in msgs if (not m.out) and m.id > after_id],
                     key=lambda x: x.id)
        if new:
            return new
    return []


async def _answer(oa, sys_prompt, convo, question):
    chat = ChatOpenAI(
        token=oa["token"], model=oa.get("model"),
        completion_endpoint=oa.get("completion_endpoint"), system_prompt=sys_prompt,
        temperature=oa.get("temperature", 0.4),
        max_completion_tokens=oa.get("max_completion_tokens", 500))
    prompt = ("Диалог с рекрутёром:\n" + "\n".join(convo[-24:])
              + f"\n\nОтветь на последний вопрос рекрутёра: «{question[:1000]}»")
    return ((await chat.send_message(prompt)) or "").strip()


async def _run_interview(client, entity, token, sys_prompt, oa):
    """Пройти одно интервью от /start до завершения. -> (status, turns)."""
    sent = await client.send_message(entity, f"/start {token}")
    last_id = sent.id
    convo, turns = [], 0
    while turns < MAX_TURNS:
        replies = await _wait_reply(client, entity, last_id)
        if not replies:
            return "done", turns  # бот молчит -> интервью завершено/пауза
        for m in replies:
            t = (m.text or "").strip()
            if t:
                convo.append("Рекрутёр: " + t)
            last_id = max(last_id, m.id)
        bot_text = "\n".join((m.text or "").strip() for m in replies).strip()
        if not bot_text:
            continue
        if DONE_RE.search(bot_text) and "?" not in bot_text:
            return "done", turns
        answer = await _answer(oa, sys_prompt, convo, bot_text)
        if not answer:
            return "done", turns
        answer = answer[:MAX_ANSWER_CHARS]
        convo.append("Я: " + answer)
        s = await client.send_message(entity, answer, link_preview=False)
        last_id = max(last_id, s.id)
        turns += 1
        print(f"  giga[{_label()}] ход {turns}: Q={bot_text[:50]!r} A={answer[:50]!r}")
    return "done", turns


async def main() -> None:
    if not pgconn.feature_enabled("giga"):
        print("feat.giga выключен — пропуск giga_recruiter")
        return
    cfg = pgconn.app_config()
    enc_sess = cfg.get("tg_user_session")
    if not enc_sess:
        print("giga: Telegram не подключён (нет tg_user_session) — пропуск")
        return
    oa = cfg.get("openai") or {}
    token = cfg.get("token") or {}
    if not oa.get("token") or not token.get("access_token"):
        print("giga: нет openai/hh токена — пропуск")
        return

    account = pgconn.get_account()
    lock_conn, got = _lock(account)
    if not got:
        lock_conn.close()
        print("giga: уже выполняется для этого аккаунта — пропуск")
        return

    sys_prompt = SYS_TMPL.format(name=_label(), resume=(cfg.get("resume_text") or "").strip())
    bot_username = pgconn.get_setting("giga.bot", DEFAULT_BOT) or DEFAULT_BOT
    api_id, api_hash = pgconn.tg_api()
    client = TelegramClient(StringSession(pgconn.dec_session(enc_sess)), api_id, api_hash)

    try:
        # 1) ПОИСК новых приглашений в hh
        new = await _discover(token, account)
        if new:
            print(f"giga: новых приглашений в очередь: {new}")

        if not _next_pending(account):
            print("giga: очередь интервью пуста — нечего проходить")
            return
        if DRY:
            row = _next_pending(account)
            print(f"DRY — есть pending интервью (token={row[0][:12]}…, {row[1]}), "
                  "не запускаю прохождение")
            return

        # 2) ПРОХОЖДЕНИЕ по очереди в рамках бюджета времени
        await client.connect()
        if not await client.is_user_authorized():
            print("giga: сессия слетела — пропуск (НЕ перезаписываю)")
            return
        entity = await client.get_entity(bot_username)

        deadline = time.time() + RUN_BUDGET_SEC
        done_n = 0
        while time.time() < deadline:
            row = _next_pending(account)
            if not row:
                break
            tok, vac = row
            _set_status(account, tok, "in_progress")
            print(f"giga: начинаю интервью «{vac}» (token={tok[:12]}…)")
            try:
                status, turns = await _run_interview(client, entity, tok, sys_prompt, oa)
                _set_status(account, tok, status, turns)
                done_n += 1
                print(f"giga: интервью «{vac}» -> {status} ({turns} ходов)")
            except Exception as e:
                _set_status(account, tok, "failed")
                print(f"giga: интервью «{vac}» упало: {repr(e)[:160]}")
        print(f"giga: за прогон пройдено интервью: {done_n}")
    except Exception as e:
        print(f"giga: непредвиденная ошибка: {repr(e)[:200]}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        _unlock(lock_conn, account)


if __name__ == "__main__":
    asyncio.run(main())
