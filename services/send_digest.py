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
                "WHERE account=%s AND sent_at IS NULL ORDER BY priority, created_at",
                (pgconn.get_account(),),
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
                "UPDATE notifications SET sent_at = now() "
                "WHERE account=%s AND id = ANY(%s)",
                (pgconn.get_account(), ids),
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
    if not pgconn.feature_enabled("notify"):
        print("feat.notify выключен в Mini App — дайджест пропущен")
        return
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
    token = tg.get("token")
    # шлём в ЛИЧКУ привязанному пользователю (tg_user_id), не в группу/топик
    dm = cfg.get("tg_user_id")
    if not (token and dm):
        print("Пользователь не привязан (/link) или нет токена — дайджест в очереди.")
        return

    if await tg_send(token, dm, msg, None):
        mark_sent(ids)
        print(f"дайджест отправлен в личку ({len(rows)} уведомл.), помечено sent.")
    else:
        print("дайджест НЕ отправлен (ошибка TG) — останется в очереди.")


if __name__ == "__main__":
    asyncio.run(main())
