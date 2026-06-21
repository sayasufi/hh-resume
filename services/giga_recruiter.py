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
from telethon.tl.functions.messages import StartBotRequest

from hh_applicant_tool.ai import ChatOpenAI
from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn

DRY = "--dry" in sys.argv

DEFAULT_BOT = "Giga_recruiter_bot"
SCAN_CAP = 120                # сколько НЕпросмотренных переписок сканировать за прогон
RUN_BUDGET_SEC = 1500         # бот медленный (1-2.5 мин/ответ), одно интервью ~15-25 мин
MAX_TURNS = 150               # потолок ходов на ВЕСЬ прогон (несколько интервью подряд)
REPLY_TIMEOUT = 210           # бот тупит до ~3 мин на каждый ответ
POLL_EVERY = 5
MAX_ANSWER_CHARS = 1500
BETWEEN_INTERVIEWS = 8        # пауза между интервью, чтобы не выглядело как флуд
MAX_ATTEMPTS = 5              # сколько раз пробуем токен, прежде чем пометить failed
MENU_MAX_PAGES = 25          # сколько страниц «Далее» листать в поиске непройденной вакансии
_SEL_PREFIX = b"SCR:SEL:"    # callback кнопки выбора вакансии (новый гига-бот)
_PAGE_PREFIX = b"SCR:PAGE:"  # callback «Далее» (пагинация меню вакансий)

GIGA_LINK_RE = re.compile(r"Giga_recruiter_bot\?start=([A-Za-z0-9_\-]+)", re.I)
# интервью реально завершено
DONE_RE = re.compile(
    r"спасибо за интервью|переда[мдл].{0,30}(резюме|рекрут|итог)|на этом (всё|все)|"
    r"всего доброго|до свидания|спасибо за обратную связь", re.I)
# активного интервью нет / отклик уже сдан
NOACTIVE_RE = re.compile(
    r"диалог по вакансии заверш|отклик уже у рекрут|помогаю только на этапе первичн|"
    r"нет новых вакансий|новых вакансий (пока )?нет|нет активных|"
    r"неполадк|передано рекрут", re.I)
# филлер — подождать, не отвечать
WAIT_RE = re.compile(
    r"нужно немного времени|ничего не писать|скоро продолж|обрабатыва|секундоч|подожд", re.I)

SYS_TMPL = (
    "Ты — кандидат {name}, проходишь первичное интервью с AI-рекрутёром (ГигаРекрутер) "
    "в Telegram. Это ЖИВОЙ ЧАТ, а не сопроводительное письмо. Отвечай ОТ ПЕРВОГО ЛИЦА "
    "ПРОСТЫМ человеческим разговорным языком — как пишешь нормальному человеку в мессенджере: "
    "короткие обычные фразы (1-3 предложения), по делу, спокойно и уверенно, ОДНИМ сообщением.\n"
    "ОПИРАЙСЯ ТОЛЬКО на свой реальный опыт (ниже). Честность и консистентность:\n"
    "— НЕ приписывай себе инструменты/фреймворки/языки, которых НЕТ в опыте ниже "
    "(напр. LangChain, LangGraph, LlamaIndex, Kubernetes, Terraform, Ansible, AWS, Golang). "
    "Про такие отвечай ОДИНАКОВО в каждом вопросе — нельзя в одном ответе «работал», а в другом «нет»;\n"
    "— чего не делал — спокойно «Нет, с этим не работал»; можно одной фразой связать со смежным "
    "опытом, но «зато быстро освою» — не чаще раза за интервью, не лепи в каждый ответ;\n"
    "— не выдумывай опыт, ссылки, GitHub, награды и цифры; просят ссылку/портфолио — скажи, что "
    "покажешь код и детали на следующем этапе.\n"
    "КАК ГОВОРИТЬ (важно):\n"
    "— живой человеческий язык, простые слова, короткие фразы. БЕЗ канцелярита и пустых "
    "бузвордов: «масштабные задачи», «проекты полного цикла», «сильная инженерная культура», "
    "«экспертиза», «синергия», «погружение», «реализация решений» — так НЕ пиши. Лучше «хочу "
    "делать интересные задачи и видеть, что моя работа реально нужна», чем «стремлюсь к "
    "реализации масштабных проектов полного цикла»;\n"
    "— говори конкретно и по-простому: что делал, на чём, что вышло (напр. «вынес тяжёлые "
    "задачи в Celery — ответ стал быстрее»), без пафоса и воды;\n"
    "— бери примеры из РАЗНОГО опыта (бэкенд под нагрузкой, телефония и failover, Celery/Kafka, "
    "ETL, REST/gRPC, базы, RAG/поиск) под вопрос и эту вакансию — не своди всё к одному (RAG/Milvus);\n"
    "— не используй слово «резюме»; не пиши заглушки в скобках [...]; не здоровайся повторно "
    "внутри одного интервью; по-русски, без markdown.\n"
    "Если просят согласие/готовность — соглашайся. Желаемая зарплата — конкретно: {salary}.\n\n"
    "=== ТВОЙ ОПЫТ ===\n{resume}\n=== КОНЕЦ ===")


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


def _mark_one_done(account):
    """Пометить done самый старый незакрытый токен (одно завершённое интервью)."""
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE giga_queue SET status='done', updated_at=now() WHERE account=%s "
                "AND token=(SELECT token FROM giga_queue WHERE account=%s AND "
                "status IN ('pending','in_progress') ORDER BY created_at LIMIT 1)",
                (account, account))
        conn.commit()
    finally:
        conn.close()


def _mark_all_done(account):
    """Бот сказал «вакансий нет» -> всё оставшееся помечаем done."""
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE giga_queue SET status='done', updated_at=now() WHERE "
                        "account=%s AND status IN ('pending','in_progress')", (account,))
        conn.commit()
    finally:
        conn.close()


def _bump_attempt(account, token) -> int:
    """+1 к счётчику попыток токена. -> новое значение attempts. Нужен, чтобы не
    ретраить вечно интервью, которое стабильно падает (пустой ответ LLM/сбой бота)."""
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE giga_queue SET attempts=attempts+1, updated_at=now() "
                        "WHERE account=%s AND token=%s RETURNING attempts",
                        (account, token))
            row = cur.fetchone()
        conn.commit()
        return row[0] if row else 0
    finally:
        conn.close()


def _notify_manual(account, token):
    """Бот просит оставить контакт — это шаг за человека. Зовём кандидата."""
    pgconn.notify(
        pgconn.PRIORITY_HIGH,
        "🤝 ГигаРекрутер просит оставить контакт на интервью — это нужно сделать "
        "вручную: загляни в @Giga_recruiter_bot и продолжи диалог.",
        category="interview", dedup_key=f"giga_manual:{token}", account=account)


def _notify_failed(account, token):
    """Интервью не прошло автоматически после нескольких попыток — сообщаем."""
    pgconn.notify(
        pgconn.PRIORITY_MED,
        "⚠️ ГигаРекрутер: не получилось пройти интервью автоматически "
        "(несколько неудачных попыток). Можно проверить в @Giga_recruiter_bot.",
        category="action", dedup_key=f"giga_failed:{token}", account=account)


def _clear_done_action_items(account):
    """Закрыть «дела» по интервью, которые ГР уже прошёл (связь по nid с giga_queue).
    -> сколько дел закрыто."""
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE action_items SET done=true WHERE account=%s AND "
                "coalesce(done,false)=false AND nid IN (SELECT nid FROM giga_queue "
                "WHERE account=%s AND status='done' AND nid IS NOT NULL)",
                (account, account))
            n = cur.rowcount
        conn.commit()
        return n
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


def _btns(m):
    out = []
    for row in (getattr(m, "buttons", None) or []):
        for b in row:
            if getattr(b, "text", None):
                out.append(b.text)
    return out


def _menu(messages):
    """Экран ВЫБОРА вакансии нового гига-бота (кнопки SCR:SEL / SCR:PAGE).
    Возвращаем САМОЕ СВЕЖЕЕ меню в пачке (кнопки старых меню протухают — клик по ним
    даёт «Ссылка не корректна»). -> (message, [(uuid, text, data_bytes), ...], page|None)."""
    found = (None, [], None)
    for m in messages:  # messages в хронологическом порядке -> остаётся последнее (новейшее)
        sels, page = [], None
        for row in (getattr(m, "buttons", None) or []):
            for b in row:
                data = getattr(b, "data", None)
                if not data:
                    continue
                if data.startswith(_SEL_PREFIX):
                    parts = data.split(b":")
                    uuid = parts[2].decode("ascii", "ignore") if len(parts) >= 3 else data.hex()
                    sels.append((uuid, getattr(b, "text", "") or "", data))
                elif data.startswith(_PAGE_PREFIX):
                    page = data
        if sels:
            found = (m, sels, page)
    return found


def _bot_waiting(last_msg):
    """Бот ждёт нашего ответа: последнее сообщение — его незакрытый вопрос. -> (bool, text)."""
    if last_msg is None or last_msg.out:
        return False, ""
    txt = (last_msg.text or "").strip()
    if not txt or NOACTIVE_RE.search(txt):
        return False, ""
    if DONE_RE.search(txt) and "?" not in txt:
        return False, ""
    if WAIT_RE.search(txt) and "?" not in txt:
        return False, ""
    return True, txt


def _star_msg(messages):
    """Найти сообщение с инлайн-оценкой (★/☆ в кнопках). -> Message|None."""
    for m in messages:
        for b in _btns(m):
            if "★" in b or "☆" in b:
                return m
    return None


async def _rate5(star_message):
    """Поставить 5★ (последняя кнопка ряда оценки)."""
    try:
        await star_message.click(text="★★★★★")
    except Exception:
        try:
            await star_message.click(4)   # 5-я кнопка в ряду (0-индекс)
        except Exception as e:
            print(f"giga: не смог поставить оценку: {repr(e)[:100]}")


async def _run_session(client, entity, oa, sys_prompt, account, menu_token):
    """Меню-флоу нового гига-бота: открываем меню вакансий deep-link'ом
    StartBotRequest(start_param=menu_token) (plain /start теперь даёт «Ссылка не
    корректна») -> клик по непройденной вакансии (SCR:SEL) -> интервью текстом -> 5★ ->
    снова меню; «Далее» (SCR:PAGE) для пагинации. Пройденные вакансии трекаем по uuid в
    seen_keys('giga_vac'). -> (status, completed, turns)."""
    done = pgconn.seen_keys("giga_vac")
    completed, turns, pages = 0, 0, 0
    cur_uuid, cur_vac, convo, tok_turns = None, "", [], 0
    deadline = time.time() + RUN_BUDGET_SEC

    async def open_menu():
        base = await client.get_messages(entity, limit=1)
        lid = base[0].id if base else 0
        await client(StartBotRequest(bot=entity, peer=entity, start_param=menu_token))
        return lid

    last_id = await open_menu()

    while turns < MAX_TURNS and time.time() < deadline:
        replies = await _wait_reply(client, entity, last_id, REPLY_TIMEOUT)
        if not replies:
            # клик мог отредактировать меню без нового сообщения — перечитаем последнее
            base = await client.get_messages(entity, limit=3)
            replies = [m for m in reversed(base) if not m.out and m.id >= last_id]
            if not replies:
                return "done", completed, turns
        for m in replies:
            last_id = max(last_id, m.id)

        # 1) экран оценки 5★ -> ставим, вакансия пройдена, назад в меню
        sm = _star_msg(replies)
        if sm is not None:
            await _rate5(sm)
            completed += 1
            if cur_uuid:
                pgconn.add_seen("giga_vac", [cur_uuid]); done.add(cur_uuid)
                pgconn.bump_activity("giga")
            print(f"giga[{_label()}]: интервью #{completed} «{cur_vac[:40]}» -> 5★")
            cur_uuid, cur_vac, convo, tok_turns = None, "", [], 0
            await asyncio.sleep(BETWEEN_INTERVIEWS)
            last_id, pages = await open_menu(), 0
            continue

        # 2) экран ВЫБОРА вакансии -> кликаем первую непройденную / листаем «Далее»
        menu_msg, sels, page = _menu(replies)
        if sels:
            pick = next(((u, t, d) for (u, t, d) in sels if u not in done), None)
            if pick:
                cur_uuid, cur_vac, convo, tok_turns, pages = pick[0], pick[1], [], 0, 0
                print(f"giga[{_label()}]: выбираю «{cur_vac[:50]}»")
                try:
                    await menu_msg.click(data=pick[2])
                except Exception as e:
                    print(f"giga: клик по вакансии не удался: {repr(e)[:90]}")
                    pgconn.add_seen("giga_vac", [cur_uuid]); done.add(cur_uuid)
                    cur_uuid = None
                await asyncio.sleep(4)
                continue
            if page and pages < MENU_MAX_PAGES:
                pages += 1
                try:
                    await menu_msg.click(data=page)
                except Exception:
                    return "done", completed, turns
                await asyncio.sleep(3)
                continue
            return "done", completed, turns           # все доступные вакансии пройдены

        # 3) кнопка «контакт» -> человеку, вакансию пропускаем, назад в меню
        if any("контакт" in b.lower() for m in replies for b in _btns(m)):
            if cur_uuid:
                _notify_manual(account, cur_uuid)
                pgconn.add_seen("giga_vac", [cur_uuid]); done.add(cur_uuid)
            cur_uuid, cur_vac = None, ""
            await asyncio.sleep(3)
            last_id, pages = await open_menu(), 0
            continue

        # 4) вопрос интервью -> отвечаем текстом
        bot_text = "\n".join((m.text or "").strip() for m in replies
                             if (m.text or "").strip()).strip()
        if not bot_text:
            continue
        if NOACTIVE_RE.search(bot_text):              # эта вакансия уже сдана -> в меню
            if cur_uuid:
                pgconn.add_seen("giga_vac", [cur_uuid]); done.add(cur_uuid)
            cur_uuid, cur_vac = None, ""
            await asyncio.sleep(4)
            last_id, pages = await open_menu(), 0
            continue
        if DONE_RE.search(bot_text) and "?" not in bot_text:
            continue                                  # «спасибо за интервью» -> ждём звёзды
        if WAIT_RE.search(bot_text) and "?" not in bot_text:
            continue

        convo.append("Рекрутёр: " + bot_text)
        answer = (await _answer(oa, sys_prompt, convo, bot_text) or "").strip()
        if not answer:
            continue
        answer = answer[:MAX_ANSWER_CHARS]
        convo.append("Я: " + answer)
        s = await client.send_message(entity, answer, link_preview=False)
        last_id = max(last_id, s.id)
        turns += 1
        tok_turns += 1
        print(f"  giga[{_label()}] ход {turns}: Q={bot_text[:45]!r} A={answer[:45]!r}")
        await asyncio.sleep(3)
    return "budget", completed, turns


WF_ORDER = {"REMOTE": 0, "HYBRID": 1, "ON_SITE": 2, "FIELD_WORK": 3, "FLY_IN_FLY_OUT": 4}


async def _work_format(token, account):
    """Формат работы: приоритет — общая настройка кандидата (preferences.work_format),
    иначе из hh-резюме (удалёнка>гибрид>офис)."""
    from hh_applicant_tool.utils import prefs as cprefs
    _wanted = cprefs.wanted_formats((pgconn.app_config(account) or {}).get("preferences"))
    if _wanted:
        return cprefs.labels_ru(_wanted)
    rid = pgconn.get_setting("apply.resume_id", account=account)
    if not (rid and (token or {}).get("access_token")):
        return ""
    api = ApiClient(
        access_token=token["access_token"], refresh_token=token.get("refresh_token", ""),
        access_expires_at=token.get("access_expires_at", 0),
        user_agent=generate_android_useragent(), refresh_hook=pgconn.locked_token_refresh)
    try:
        r = await api.get(f"/resumes/{rid}")
        items = sorted([w for w in (r.get("work_format") or []) if w.get("name")],
                       key=lambda w: WF_ORDER.get(w.get("id"), 9))
        return ", ".join(w["name"] for w in items)
    except Exception:
        return ""
    finally:
        await api.aclose()


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

    sal = str((cfg.get("preferences") or {}).get("salary") or "").strip()
    salary_str = (f"{sal} рублей на руки" if sal.replace(" ", "").isdigit()
                  else (sal or "обсуждается на собеседовании, открыт к предложениям"))
    sys_prompt = SYS_TMPL.format(name=_label(), salary=salary_str,
                                 resume=(cfg.get("resume_text") or "").strip())
    wf = await _work_format(token, account)
    if wf:
        sys_prompt += (
            f"\n\nФорматы работы, которые тебе подходят (в порядке приоритета): {wf}. "
            "Если спрашивают про формат работы или что тебе удобнее — называй ИМЕННО эти форматы; "
            "самый приоритетный (первый в списке) указывай как предпочтительный, остальные — как тоже "
            "приемлемые. Форматы, которых нет в списке, не называй.")
    bot_username = pgconn.get_setting("giga.bot", DEFAULT_BOT) or DEFAULT_BOT
    api_id, api_hash = pgconn.tg_api()
    client = TelegramClient(StringSession(pgconn.dec_session(enc_sess)), api_id, api_hash)

    try:
        # 1) ПОИСК новых приглашений в hh
        new = await _discover(token, account)
        if new:
            print(f"giga: новых приглашений в очередь: {new}")

        _row = _next_pending(account)
        if not _row:
            print("giga: очередь интервью пуста — нечего проходить")
            return
        menu_token = _row[0]   # любой токен открывает меню вакансий (deep-link-ключ)
        if DRY:
            print(f"DRY — есть pending интервью (token={menu_token[:12]}…, {_row[1]}), "
                  "не запускаю прохождение")
            return

        # 2) ПРОХОЖДЕНИЕ: одна непрерывная сессия — бот ведёт интервью за интервью сам
        await client.connect()
        if not await client.is_user_authorized():
            print("giga: сессия слетела — пропуск (НЕ перезаписываю)")
            return
        entity = await client.get_entity(bot_username)

        status, completed, turns = await _run_session(
            client, entity, oa, sys_prompt, account, menu_token)
        # триггер-токены (giga-ссылки из hh-чатов) отработали свою роль — помечаем done,
        # чтобы не перезапускать; реальные интервью трекаются по uuid (seen_keys 'giga_vac').
        _mark_all_done(account)
        cleared = _clear_done_action_items(account)
        if cleared:
            print(f"giga: закрыто дел по пройденным интервью: {cleared}")
        print(f"giga: прогон завершён: status={status}, интервью={completed}, ходов={turns}")
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
