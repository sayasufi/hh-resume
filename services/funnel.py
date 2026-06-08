"""Воронка откликов -> 🟢 уведомление в дайджест. Считает отклики по статусам
(всего/ответы/интервью/приглашения/отказы), в т.ч. в разрезе резюме. Это аналитика
«для глаз» — НИЧЕГО не фильтрует и не откидывает. Cron раз в день.

Запуск: python funnel.py [--dry]   (обычно через run_all)
"""
import asyncio
import collections
import datetime as dt
import sys

from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn

DRY = "--dry" in sys.argv


async def main():
    cfg = pgconn.app_config()
    tok = cfg.get("token") or {}
    if not tok.get("access_token"):
        print("funnel: нет токена")
        return

    api = ApiClient(
        access_token=tok["access_token"],
        refresh_token=tok["refresh_token"],
        access_expires_at=tok.get("access_expires_at", 0),
        user_agent=generate_android_useragent(),
        refresh_hook=pgconn.locked_token_refresh,
    )
    by_state = collections.Counter()
    by_resume = collections.defaultdict(collections.Counter)
    titles = {}
    try:
        try:
            res = await api.get("/resumes/mine")
            titles = {r["id"]: (r.get("title") or "?") for r in res.get("items", [])}
        except Exception:
            pass
        page = 0
        while True:
            r = await api.get("/negotiations", page=page, per_page=100, status="all")
            its = r.get("items", [])
            if not its:
                break
            for n in its:
                st = n.get("state", {}).get("id")
                by_state[st] += 1
                rid = (n.get("resume") or {}).get("id")
                by_resume[rid][st] += 1
            if page + 1 >= r.get("pages", 0):
                break
            page += 1
    finally:
        await api.aclose()

    total = sum(by_state.values())
    if not total:
        print("funnel: откликов нет")
        return

    def fnum(c, k):
        return c.get(k, 0)

    lines = [
        f"Воронка — {total} откликов",
        f"💬 ответили {fnum(by_state,'response')} · 🎯 интервью {fnum(by_state,'interview')} · "
        f"📩 приглашений {fnum(by_state,'invitation')} · ❌ отказов {fnum(by_state,'discard')}",
        "",
        "По резюме (🎯 интервью · 📩 приглаш · 💬 ответы · ❌ отказы):",
    ]
    # по резюме, сверху — где больше «горячих» (интервью+приглашения)
    ranked = sorted(
        by_resume.items(),
        key=lambda kv: -(fnum(kv[1], "interview") + fnum(kv[1], "invitation")),
    )
    for rid, c in ranked:
        t = titles.get(rid) or ("без резюме" if rid is None else f"резюме {str(rid)[:8]}")
        t = " ".join(t.split())  # схлопнуть переносы/двойные пробелы
        if len(t) > 46:
            t = t[:45].rstrip() + "…"
        lines.append(
            f"• {t}: {sum(c.values())} → 🎯{fnum(c,'interview')} "
            f"📩{fnum(c,'invitation')} 💬{fnum(c,'response')} ❌{fnum(c,'discard')}"
        )
    text = "\n".join(lines)

    if DRY:
        print("DRY:\n" + text)
        return

    pgconn.notify(
        pgconn.PRIORITY_LOW, text, category="funnel",
        dedup_key=f"funnel:{dt.date.today().isoformat()}",
    )
    print("воронка поставлена в очередь")


if __name__ == "__main__":
    asyncio.run(main())
