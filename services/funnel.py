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

# Состояния hh -> (эмодзи, подпись, ранг) для dlg_cache (как в web_app._NEG_STATE)
_NEG_STATE = {
    "hired": ("🎉", "Оффер", 0),
    "interview": ("🤝", "Собеседование", 1),
    "invitation": ("🤝", "Собеседование", 1),
    "response": ("💬", "Ответ", 2), "discard": ("🔴", "Отказ", 4),
    "discard_by_applicant": ("⚪️", "Отозван", 5), "hidden": ("⚪️", "Скрыт", 5),
}


def _persist(account, neg_rows, dlg_rows):
    """Сохранить ПОЛНУЮ воронку аккаунта в БД (раньше числа уходили только в текст
    уведомления): public.negotiations — полный срез без капа (id/state/vacancy/resume),
    + обновить dlg_cache, чтобы кабинет и аналитика были свежими ЕЖЕДНЕВНО для всех
    аккаунтов, не завися от открытия Mini App. Пишется в отдельном потоке."""
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO negotiations(id, account, state, vacancy_id, employer_id, "
                "chat_id, resume_id, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET account=excluded.account, "
                "state=excluded.state, vacancy_id=excluded.vacancy_id, "
                "employer_id=excluded.employer_id, resume_id=excluded.resume_id, "
                "updated_at=excluded.updated_at",
                neg_rows)
            cur.executemany(
                "INSERT INTO dlg_cache(account, nid, title, employer, state_id, state, "
                "emoji, rank, has_updates, url, updated, created, ts) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) "
                "ON CONFLICT(account, nid) DO UPDATE SET title=excluded.title, "
                "employer=excluded.employer, state_id=excluded.state_id, "
                "state=excluded.state, emoji=excluded.emoji, rank=excluded.rank, "
                "has_updates=excluded.has_updates, url=excluded.url, "
                "updated=excluded.updated, "
                "created=COALESCE(excluded.created, dlg_cache.created), ts=now()",
                dlg_rows)
        conn.commit()
    finally:
        conn.close()


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
    account = pgconn.get_account()
    by_state = collections.Counter()
    by_resume = collections.defaultdict(collections.Counter)
    titles = {}
    neg_rows, dlg_rows = [], []
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
                st = (n.get("state") or {}).get("id")
                by_state[st] += 1
                vac = n.get("vacancy") or {}
                emp = vac.get("employer") or {}
                rid = (n.get("resume") or {}).get("id")
                by_resume[rid][st] += 1
                emoji, label, rank = _NEG_STATE.get(
                    st, ("•", (n.get("state") or {}).get("name") or st or "", 2))
                nid = n.get("id")
                neg_rows.append((
                    nid, account, st, vac.get("id"), emp.get("id"),
                    n.get("chat_id"), rid, n.get("created_at"), n.get("updated_at"),
                ))
                dlg_rows.append((
                    account, str(nid), vac.get("name") or "Вакансия",
                    emp.get("name") or "", st, label, emoji, rank,
                    bool(n.get("has_updates")), vac.get("alternate_url") or "",
                    (n.get("updated_at") or "")[:10], (n.get("created_at") or "")[:10],
                ))
            if page + 1 >= r.get("pages", 0):
                break
            page += 1
    finally:
        await api.aclose()

    if not DRY and neg_rows:
        try:
            await asyncio.to_thread(_persist, account, neg_rows, dlg_rows)
            print(f"funnel: сохранено в БД negotiations/dlg_cache: {len(neg_rows)}")
        except Exception as ex:
            print(f"funnel: не удалось сохранить в БД: {ex!r}")

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
