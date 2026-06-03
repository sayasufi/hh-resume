"""Отправка единого приоритизированного дайджеста уведомлений в Telegram.

Берёт неотправленные строки из notifications (схема юзера), сортирует по важности
(🔴→🟡→🟢) и шлёт ОДНИМ сообщением в топик юзера, затем помечает sent_at.
Источники (reply_employers/notify_actions/monitor/apply_tests) только КЛАДУТ
уведомления через pgconn.notify(); отправляет — только этот скрипт (cron */30).

Запуск:  python send_digest.py [--dry]   (обычно через run_all)
"""
import asyncio
import os
import sys

from aiogram import Bot

from hh_applicant_tool.storage import pgconn

DRY = "--dry" in sys.argv

# приоритет -> (эмодзи, заголовок блока)
BLOCKS = {
    pgconn.PRIORITY_HIGH: ("🔴", "ВАЖНОЕ — нужен ты"),
    pgconn.PRIORITY_MED: ("🟡", "ДЕЛА"),
    pgconn.PRIORITY_LOW: ("🟢", "ИНФО"),
}


def _user_label() -> str:
    try:
        return pgconn.get_setting("user.full_name") or pgconn.get_account()
    except Exception:
        return pgconn.get_account()


def fetch_unsent() -> list[tuple]:
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, priority, text, link FROM notifications "
                "WHERE sent_at IS NULL ORDER BY priority, created_at"
            )
            return cur.fetchall()
    finally:
        conn.close()


def mark_sent(ids: list[int]) -> None:
    if not ids:
        return
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE notifications SET sent_at = now() WHERE id = ANY(%s)",
                (ids,),
            )
        conn.commit()
    finally:
        conn.close()


def build_message(rows: list[tuple]) -> str:
    who = _user_label()
    lines = [f"👤 {who}  ·  уведомлений: {len(rows)}"]
    last_prio = None
    n = 0
    for _id, prio, text, link in rows:
        if prio != last_prio:
            emoji, title = BLOCKS.get(prio, ("•", "ПРОЧЕЕ"))
            lines.append("")           # пустая строка перед блоком
            lines.append(f"{emoji} {title}")
            last_prio = prio
            n = 0
        n += 1
        lines.append("")               # пустая строка между пунктами
        lines.append(f"{n}. {text}")
        if link:
            lines.append(f"   🔗 {link}")
    return "\n".join(lines)


async def tg_send(token: str, chat_id, text: str, topic_id=None) -> bool:
    bot = Bot(token)
    ok = True
    try:
        for i in range(0, len(text), 3800):
            try:
                await bot.send_message(
                    chat_id, text[i:i + 3800], message_thread_id=topic_id
                )
            except Exception as e:
                print("TG error:", repr(e)[:160])
                ok = False
            await asyncio.sleep(0.4)
    finally:
        await bot.session.close()
    return ok


async def main() -> None:
    rows = fetch_unsent()
    if not rows:
        print("дайджест: нет новых уведомлений")
        return

    msg = build_message(rows)
    ids = [r[0] for r in rows]

    if DRY:
        print("DRY — дайджест не отправлен:\n" + msg)
        return

    cfg = pgconn.app_config()
    tg = cfg.get("telegram") or {}
    if not (tg.get("token") and tg.get("chat_id")):
        print("Telegram не настроен — дайджест не отправлен (останется в очереди).")
        return

    if await tg_send(tg["token"], tg["chat_id"], msg, tg.get("topic_id")):
        mark_sent(ids)
        print(f"дайджест отправлен ({len(rows)} уведомл.), помечено sent.")
    else:
        print("дайджест НЕ отправлен (ошибка TG) — останется в очереди.")


if __name__ == "__main__":
    asyncio.run(main())
