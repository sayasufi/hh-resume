"""Мониторинг/heartbeat одного юзера (через run_all -> на каждого).
Проверяет токен, vLLM, активность и КЛАДЁТ результат в очередь уведомлений
(pgconn.notify); отправит send_digest. Dead-man-switch: ежедневный 🟢 «бот жив»
(если в дайджесте его нет — что-то сломалось). Проблемы идут как 🔴/🟡.
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
        return pgconn.get_setting("user.full_name") or pgconn.get_account()
    except Exception:
        return pgconn.get_account()


async def main():
    cfg = pgconn.app_config()
    today = dt.date.today().isoformat()
    problems = []   # (priority, text)
    info = []       # строки для 🟢 heartbeat

    # 1) токен
    tok = cfg.get("token") or {}
    exp = tok.get("access_expires_at", 0)
    days_left = (exp - time.time()) / 86400 if exp else -1
    if days_left < 0:
        problems.append((pgconn.PRIORITY_HIGH, "токен ИСТЁК — нужна переавторизация"))
    elif days_left < 2:
        problems.append((pgconn.PRIORITY_MED, f"токен истекает через {days_left:.1f} дн"))
    # токен ок -> в дайджест кандидата не пишем (операционная телеметрия)

    # 2) vLLM
    oa = cfg.get("openai") or {}
    ep = oa.get("completion_endpoint", "")
    if ep:
        base = ep.rsplit("/chat/completions", 1)[0]
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(base + "/models")
                r.json()  # проверка доступности vLLM; модель кандидату не показываем
        except Exception:
            problems.append((pgconn.PRIORITY_MED, "vLLM недоступен (письма/ответы в шаблон)"))

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
        # храним телефон hh-профиля (для сопоставления Telegram<->hh в /connect)
        try:
            pgconn.set_app_config("hh_phone", me.get("phone") or "")
        except Exception:
            pass
        cnt = me.get("counters", {})
        info.append(f"👀 новых просмотров резюме: +{cnt.get('new_resume_views', 0)}")
        info.append(f"🤝 новых приглашений: +{cnt.get('unread_negotiations', 0)}")
    except Exception as e:
        problems.append((pgconn.PRIORITY_MED, f"hh API: {repr(e)[:50]}"))

    cnt_today = pgconn.get_setting("_applications_count") or "?"
    dat = pgconn.get_setting("_applications_date")
    info.append(f"📨 откликов сегодня: {cnt_today if dat == today else 0}")

    # Проблемы -> отдельные 🔴/🟡 уведомления (dedup по дню).
    for prio, text in problems:
        pgconn.notify(
            prio, f"мониторинг: {text}", category="monitor",
            dedup_key=f"monitor:{prio}:{text[:20]}:{today}",
        )
    # Ежедневный heartbeat -> 🟢 (dead-man-switch).
    head = "✅ Бот работает" if not problems else "⚠️ Бот работает (есть проблема — детали выше)"
    pgconn.notify(
        pgconn.PRIORITY_LOW, head + "\n" + "\n".join(info),
        category="heartbeat", dedup_key=f"heartbeat:{today}",
    )
    print(f"монитор: проблем {len(problems)}, heartbeat поставлен.")


if __name__ == "__main__":
    asyncio.run(main())
