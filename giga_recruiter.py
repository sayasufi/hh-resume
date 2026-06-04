#!/usr/bin/env python3
"""ГигаРекрутер-автоответчик (standalone, per-account через run_all.py).

Грузит Telethon-сессию аккаунта (привязанную в /connect), читает диалог с ботом
@Giga_recruiter_bot, и на НОВЫЙ вопрос рекрутёра генерирует ответ от лица кандидата
через ChatOpenAI и АВТОМАТИЧЕСКИ отправляет его в чат ГР. Без копии в личку.

Гейт: feature_enabled('giga') И наличие app_config['tg_user_session'].
Отправляет ТОЛЬКО в чат @Giga_recruiter_bot, на чужие сообщения (out=False).
Дедуп по message.id (seen_keys 'giga'). Advisory-lock на аккаунт — от двойного ответа.

Запуск:  python giga_recruiter.py [--dry]   (обычно через run_all)
"""
import asyncio
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

from hh_applicant_tool.ai import ChatOpenAI
from hh_applicant_tool.storage import pgconn

DRY = "--dry" in sys.argv

DEFAULT_BOT = "Giga_recruiter_bot"
HISTORY_LIMIT = 20
MAX_ANSWER_CHARS = 1500
SEEN_KIND = "giga"

SYS_TMPL = (
    "Ты — кандидат {name}. Тебе пишет автоматический рекрутёр (ГигаРекрутер) в "
    "Telegram в рамках первичного интервью. Отвечай ОТ ПЕРВОГО ЛИЦА, кратко, "
    "уверенно и по делу на ПОСЛЕДНИЙ вопрос рекрутёра, опираясь СТРОГО на факты из "
    "своего резюме ниже. Не выдумывай опыт, которого нет в резюме. Без приветствий "
    "в каждом сообщении, не повторяй уже сказанное. Пиши на русском, без markdown.\n\n"
    "=== РЕЗЮМЕ ===\n{resume}\n=== КОНЕЦ РЕЗЮМЕ ==="
)


def _label() -> str:
    try:
        return pgconn.get_setting("user.full_name") or pgconn.get_account()
    except Exception:
        return pgconn.get_account()


def _lock(account: str):
    """Advisory-lock на аккаунт. -> (conn, got). conn держать до конца работы."""
    conn = pgconn.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (f"giga:{account}",))
        got = cur.fetchone()[0]
    return conn, got


def _unlock(conn, account: str) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"giga:{account}",))
    except Exception:
        pass
    finally:
        conn.close()


async def main() -> None:
    if not pgconn.feature_enabled("giga"):
        print("feat.giga выключен — пропуск giga_recruiter")
        return

    cfg = pgconn.app_config()
    enc_sess = cfg.get("tg_user_session")
    if not enc_sess:
        print("giga: нет tg_user_session (Telegram не подключён) — пропуск")
        return
    oa = cfg.get("openai") or {}
    if not oa.get("token"):
        print("giga: нет openai.token — пропуск")
        return

    account = pgconn.get_account()
    lock_conn, got = _lock(account)
    if not got:
        lock_conn.close()
        print("giga: другой процесс уже отвечает за этот аккаунт — пропуск")
        return

    resume = (cfg.get("resume_text") or "").strip()
    bot_username = pgconn.get_setting("giga.bot", DEFAULT_BOT) or DEFAULT_BOT
    api_id, api_hash = pgconn.tg_api()
    client = TelegramClient(StringSession(pgconn.dec_session(enc_sess)), api_id, api_hash)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            print("giga: сессия слетела (не авторизована) — пропуск, НЕ перезаписываю")
            return

        try:
            entity = await client.get_entity(bot_username)
        except Exception as e:
            print(f"giga: бот @{bot_username} не найден: {repr(e)[:140]}")
            return

        messages = await client.get_messages(entity, limit=HISTORY_LIMIT)
        if not messages:
            print("giga: пустой диалог — пропуск")
            return
        if messages[0].out:
            print("giga: последнее сообщение наше — ждём вопрос рекрутёра")
            return

        last_in = messages[0]  # самый свежий вопрос рекрутёра (out=False)
        if str(last_in.id) in pgconn.seen_keys(SEEN_KIND):
            print("giga: последний вопрос уже обработан — пропуск")
            return
        question = (last_in.text or "").strip()
        if not question:
            if not DRY:
                pgconn.add_seen(SEEN_KIND, [last_in.id])
            print("giga: входящее без текста — помечено seen, пропуск")
            return

        convo = []
        for m in reversed(messages):  # старые -> новые
            t = (m.text or "").strip()
            if t:
                convo.append(("Я" if m.out else "Рекрутёр") + ": " + t)
        prompt = (
            "Диалог с рекрутёром (последние реплики):\n" + "\n".join(convo[-20:])
            + f"\n\nОтветь на ПОСЛЕДНИЙ вопрос рекрутёра: «{question}»"
        )

        chat = ChatOpenAI(
            token=oa["token"], model=oa.get("model"),
            completion_endpoint=oa.get("completion_endpoint"),
            system_prompt=SYS_TMPL.format(name=_label(), resume=resume),
            temperature=oa.get("temperature", 0.4),
            max_completion_tokens=oa.get("max_completion_tokens", 500),
        )
        try:
            answer = (await chat.send_message(prompt) or "").strip()
        except Exception as e:
            print(f"giga: LLM упал ({repr(e)[:140]}) — НЕ seen, повтор позже")
            return
        if not answer:
            print("giga: LLM вернул пусто — повтор позже")
            return
        answer = answer[:MAX_ANSWER_CHARS]
        print(f"giga[{_label()}] Q={question[:70]!r} -> A={answer[:70]!r}")

        if DRY:
            print("DRY — не отправлено, seen не записан")
            return

        try:  # отправка ТОЛЬКО в тот же entity бота ГР
            await client.send_message(entity, answer, link_preview=False)
        except Exception as e:
            print(f"giga: отправка упала ({repr(e)[:140]}) — НЕ seen, повтор")
            return
        pgconn.add_seen(SEEN_KIND, [last_in.id])  # дедуп строго ПОСЛЕ отправки
        print("giga: ответ отправлен в чат ГР, помечен seen")
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
