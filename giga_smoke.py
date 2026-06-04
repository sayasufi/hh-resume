#!/usr/bin/env python3
"""Проводит ОДНО интервью ГигаРекрутера для HH_ACCOUNT (правильный механизм).

- Если бот уже ждёт ответ (последнее сообщение — вопрос) -> продолжаем текущее.
- Иначе StartBotRequest(start_param=token) — диплинк, как клик по ссылке hh.
  ВАЖНО: бот игнорит новый /start, пока не закрыто предыдущее интервью.
Бот медленный (1-2.5 мин). Полный транскрипт в stdout. Один интервью и стоп.
"""
import asyncio
import re
import time
import giga_recruiter as gr
from telethon.tl.functions.messages import StartBotRequest
from hh_applicant_tool.storage import pgconn

REPLY_TIMEOUT = 210
POLL_EVERY = 5
MAX_TURNS = 30
MAX_ANSWER_CHARS = 1500

DONE_RE = re.compile(
    r"спасибо за интервью|переда[мдл].{0,30}резюме|переда[мдл].{0,30}рекрут|"
    r"на этом (всё|все)|всего доброго|до свидания|спасибо за обратную связь", re.I)
NOACTIVE_RE = re.compile(
    r"диалог по вакансии заверш|отклик уже у рекрут|помогаю только на этапе первичн|"
    r"нет новых вакансий|новых вакансий (пока )?нет|нет активных", re.I)
WAIT_RE = re.compile(
    r"нужно немного времени|ничего не писать|скоро продолж|обрабатыва|секундоч|подожд", re.I)


def _btns(m):
    out = []
    for row in (getattr(m, "buttons", None) or []):
        for b in row:
            if getattr(b, "text", None):
                out.append(b.text)
    return out


async def _wait(client, entity, after_id, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(POLL_EVERY)
        msgs = await client.get_messages(entity, limit=8)
        new = sorted([m for m in msgs if (not m.out) and m.id > after_id], key=lambda x: x.id)
        if new:
            return new
    return []


async def _converse(client, entity, oa, sys_prompt, last_id, seed=None):
    convo, turns, status = [], 0, "timeout"
    pending = seed
    while turns < MAX_TURNS:
        if pending is None:
            replies = await _wait(client, entity, last_id, REPLY_TIMEOUT)
            if not replies:
                print(f"[нет ответа за {REPLY_TIMEOUT}c -> стоп]"); status = "timeout"; break
            parts, buttons = [], []
            for m in replies:
                t = (m.text or "").strip()
                if t:
                    parts.append(t); print(f"🤖 ГР: {t}")
                buttons += _btns(m)
                last_id = max(last_id, m.id)
            if buttons:
                print(f"   ⌨️ КНОПКИ: {buttons}")
            if any("контакт" in b.lower() for b in buttons):
                print("[бот просит контакт — нужна ручная привязка; стоп]"); status = "need_contact"; break
            bot_text = "\n".join(parts).strip()
            if not bot_text:
                continue
        else:
            bot_text = pending
            pending = None
            print(f"🤖 ГР(ждал ответа): {bot_text}")

        if NOACTIVE_RE.search(bot_text):
            print("[активных интервью нет / уже сдано -> стоп]"); status = "no_active"; break
        if DONE_RE.search(bot_text) and "?" not in bot_text:
            print("[интервью завершено]"); status = "done"; break
        if WAIT_RE.search(bot_text) and "?" not in bot_text:
            print("[филлер -> жду дальше]"); continue

        convo.append("Рекрутёр: " + bot_text)
        answer = (await gr._answer(oa, sys_prompt, convo, bot_text) or "").strip()
        if not answer:
            print("[LLM пусто -> стоп]"); status = "empty"; break
        answer = answer[:MAX_ANSWER_CHARS]
        convo.append("Я: " + answer)
        print(f"💬 Я: {answer}")
        s = await client.send_message(entity, answer, link_preview=False)
        last_id = max(last_id, s.id)
        turns += 1
        await asyncio.sleep(3)
    return status, turns


async def main():
    account = pgconn.get_account()
    cfg = pgconn.app_config()
    enc = cfg.get("tg_user_session")
    if not enc:
        print("нет tg_user_session"); return
    oa = cfg.get("openai") or {}
    _sal = str((cfg.get("preferences") or {}).get("salary") or "").strip()
    _salary = (f"{_sal} рублей на руки" if _sal.replace(" ", "").isdigit()
               else (_sal or "обсуждается на собеседовании"))
    sys_prompt = gr.SYS_TMPL.format(name=gr._label(), salary=_salary,
                                    resume=(cfg.get("resume_text") or "").strip())
    bot = pgconn.get_setting("giga.bot", gr.DEFAULT_BOT) or gr.DEFAULT_BOT
    api_id, api_hash = pgconn.tg_api()

    lock_conn, got = gr._lock(account)
    if not got:
        lock_conn.close(); print("giga-lock занят — стоп"); return

    client = gr.TelegramClient(gr.StringSession(pgconn.dec_session(enc)), api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print("сессия не авторизована — стоп"); return
        entity = await client.get_entity(bot)

        last = await client.get_messages(entity, limit=1)
        last_msg = last[0] if last else None
        last_text = (last_msg.text or "").strip() if last_msg else ""
        last_id = last_msg.id if last_msg else 0
        bot_waiting = (
            last_msg is not None and not last_msg.out and bool(last_text)
            and not NOACTIVE_RE.search(last_text)
            and not (DONE_RE.search(last_text) and "?" not in last_text)
            and not (WAIT_RE.search(last_text) and "?" not in last_text)
        )

        if bot_waiting:
            print(f"=== ПРОДОЛЖАЮ текущее интервью (бот ждёт ответ), account={account} ===")
            status, turns = await _converse(client, entity, oa, sys_prompt, last_id, seed=last_text)
            vac = "(текущее интервью)"
        else:
            row = gr._next_pending(account)
            if not row:
                print("очередь пуста"); return
            tok, vac = row
            gr._set_status(account, tok, "in_progress")
            print(f"=== НОВОЕ интервью, account={account}, вакансия={vac!r} ===")
            print(f">>> StartBot ?start={tok}")
            await client(StartBotRequest(bot=entity, peer=entity, start_param=tok))
            status, turns = await _converse(client, entity, oa, sys_prompt, last_id)
            gr._set_status(account, tok, status, turns)

        print(f"\n=== ИТОГ: status={status}, ходов={turns}, вакансия={vac} ===")
    except Exception as e:
        print(f"[ошибка] {repr(e)[:220]}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        gr._unlock(lock_conn, account)


if __name__ == "__main__":
    asyncio.run(main())
