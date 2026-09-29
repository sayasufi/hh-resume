"""Напоминания по горячим чатам: приглашение на живой разговор бот передал тебе (хэндофф,
reply-employers), а твоего ответа в чате всё нет. Раньше такие чаты молчали до конца —
29.09.2026 два приглашения (созвон и слот в календаре) висели без ответа.

Для каждого хэндоффа за 7 дней: последнее слово за работодателем -> 🔴 напоминание через
3 ч, сутки и 3 дня (utils/handoff.STAGES); ты ответил / отказ / писать нельзя — больше
не напоминаем. Только для аккаунтов, где бот ведёт чаты (feat.reply), — чтение чата
снимает «непрочитано», а при выключенном reply владелец читает сам.

Запуск: python remind_handoffs.py [--dry]
"""
import asyncio
import sys
from datetime import datetime

from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn
from hh_applicant_tool.utils import handoff
from hh_applicant_tool.utils.date import parse_api_datetime

DRY = "--dry" in sys.argv
LOOKBACK_DAYS = 7


def _handoffs(account: str) -> list[str]:
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key FROM seen_keys WHERE account=%s AND kind='handoff' "
                        "AND created_at > now() - make_interval(days => %s)",
                        (account, LOOKBACK_DAYS))
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


async def main():
    if not (pgconn.feature_enabled("reply") and pgconn.feature_enabled("notify")):
        print("remind_handoffs: feat.reply/notify выключен — пропуск")
        return
    account = pgconn.get_account()
    tok = pgconn.app_config().get("token") or {}
    if not tok.get("access_token"):
        return
    nids = _handoffs(account)
    if not nids:
        print("remind_handoffs: горячих чатов нет")
        return
    sent_keys = pgconn.seen_keys("handoff_remind")
    api = ApiClient(access_token=tok["access_token"], refresh_token=tok.get("refresh_token"),
                    access_expires_at=tok.get("access_expires_at", 0),
                    user_agent=generate_android_useragent(),
                    refresh_hook=pgconn.locked_token_refresh)
    reminded = closed = 0
    try:
        for nid in nids:
            if f"{nid}:done" in sent_keys:
                continue
            try:
                neg = await api.get(f"/negotiations/{nid}")
                m = await api.get(f"/negotiations/{nid}/messages", per_page=100)
                if (m.get("pages") or 1) > 1:
                    m = await api.get(f"/negotiations/{nid}/messages", per_page=100,
                                      page=m["pages"] - 1)
            except Exception as ex:
                print("remind_handoffs:", nid, type(ex).__name__)
                continue
            msgs = m.get("items") or []
            state = (neg.get("state") or {}).get("id")
            answered = bool(msgs) and (msgs[-1].get("author") or {}).get(
                "participant_type") != "employer"
            if answered or state == "discard" or (neg.get("messaging_status") or "ok") != "ok":
                closed += 1
                if not DRY:
                    pgconn.add_seen("handoff_remind", f"{nid}:done")
                continue
            last_at = parse_api_datetime(msgs[-1]["created_at"]) if msgs else None
            if not last_at:
                continue
            hours = (datetime.now(last_at.tzinfo) - last_at).total_seconds() / 3600
            sent = {int(k.split(":")[1]) for k in sent_keys
                    if k.startswith(f"{nid}:") and k.split(":")[1].isdigit()}
            stage = handoff.due_stage(hours, sent)
            if not stage:
                continue
            vac = neg.get("vacancy") or {}
            text = (f"⏰ Приглашение без ответа — {stage[1]}: {vac.get('name', '')}"
                    f" ({(vac.get('employer') or {}).get('name', '')}). Ответь в чате.")
            link = f"https://hh.ru/chat/{neg.get('chat_id') or nid}"
            print(("DRY " if DRY else "") + text, link)
            reminded += 1
            if not DRY:
                pgconn.notify(pgconn.PRIORITY_HIGH, text, category="interview", link=link,
                              dedup_key=f"remind:{nid}:{stage[0]}")
                # ранние стадии тоже помечаем — их больше не шлём
                pgconn.add_seen("handoff_remind",
                                [f"{nid}:{h}" for h, _ in handoff.STAGES if h <= stage[0]])
    finally:
        await api.aclose()
    print(f"remind_handoffs: горячих {len(nids)}, напомнил {reminded}, закрыто {closed}")


if __name__ == "__main__":
    asyncio.run(main())
