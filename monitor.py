"""Мониторинг/heartbeat одного юзера (запускается через run_all -> на каждого).
Проверяет: валидность токена, доступность vLLM, активность; шлёт сводку/алерт
в Telegram-топик юзера. Dead-man-switch: если сводка не пришла — что-то сломалось.
"""
import asyncio
import datetime as dt
import os
import time

import httpx

from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn


def label():
    try:
        return pgconn.get_setting("user.full_name") or os.environ.get(
            "HH_DB_SCHEMA", "?"
        )
    except Exception:
        return os.environ.get("HH_DB_SCHEMA", "?")


async def tg(cfg, text):
    t = cfg.get("telegram") or {}
    if not (t.get("token") and t.get("chat_id")):
        return
    data = {"chat_id": t["chat_id"], "text": text}
    if t.get("topic_id"):
        data["message_thread_id"] = t["topic_id"]
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            await c.post(
                f"https://api.telegram.org/bot{t['token']}/sendMessage", data=data
            )
    except Exception as e:
        print("tg err", repr(e)[:80])


async def main():
    cfg = pgconn.app_config()
    who = label()
    alerts = []
    info = []

    # 1) токен
    tok = cfg.get("token") or {}
    exp = tok.get("access_expires_at", 0)
    days_left = (exp - time.time()) / 86400 if exp else -1
    if days_left < 0:
        alerts.append("🔴 токен ИСТЁК — нужна переавторизация")
    elif days_left < 2:
        alerts.append(f"🟠 токен истекает через {days_left:.1f} дн")
    else:
        info.append(f"токен ок ({days_left:.0f} дн)")

    # 2) vLLM
    oa = cfg.get("openai") or {}
    ep = oa.get("completion_endpoint", "")
    if ep:
        base = ep.rsplit("/chat/completions", 1)[0]
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(base + "/models")
                ids = [m["id"] for m in r.json().get("data", [])]
                info.append(f"LLM ок ({ids[0] if ids else '—'})")
        except Exception:
            alerts.append("🔴 vLLM недоступен (письма/ответы в шаблон)")

    # 3) активность за сегодня + counters
    try:
        api = ApiClient(
            access_token=tok.get("access_token"),
            refresh_token=tok.get("refresh_token"),
            access_expires_at=exp,
            user_agent=generate_android_useragent(),
            refresh_hook=pgconn.locked_token_refresh,
        )
        try:
            me = await api.get("/me")
        finally:
            await api.aclose()
        cnt = me.get("counters", {})
        info.append(
            f"приглашений +{cnt.get('unread_negotiations', 0)}, "
            f"просмотров +{cnt.get('new_resume_views', 0)}"
        )
    except Exception as e:
        alerts.append(f"🟠 hh API: {repr(e)[:50]}")

    today = dt.date.today().isoformat()
    cnt_today = pgconn.get_setting("_applications_count") or "?"
    dat = pgconn.get_setting("_applications_date")
    info.append(f"откликов сегодня: {cnt_today if dat == today else 0}")

    head = "🟢 бот жив" if not alerts else "⚠️ ВНИМАНИЕ"
    msg = f"{head} | 👤 {who}\n" + "\n".join(alerts + ["• " + i for i in info])
    print(msg)
    await tg(cfg, msg)


if __name__ == "__main__":
    asyncio.run(main())
