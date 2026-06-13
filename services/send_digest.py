"""Единый дайджест уведомлений в Telegram.

Ежедневная сводка идёт КРАСИВОЙ PNG-карточкой (HTML+CSS -> Chromium через Playwright,
уже в образе; без внешних сервисов), а кликабельные действия (живые приглашения,
анкеты, контакты) — в подписи к фото. Если рендер не удался или сводки нет (только
срочные дела) — чистый текстовый фолбэк.

Источники (reply_employers/notify_actions/monitor/funnel) только КЛАДУТ уведомления
через pgconn.notify(); отправляет — этот скрипт (cron */30). Запуск: python send_digest.py [--dry]
"""
import asyncio
import datetime as dt
import html
import os
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BufferedInputFile

from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import digest_card  # noqa: E402

DRY = "--dry" in sys.argv

_MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря"]
_SOB = ("interview", "invitation")

# приоритет -> (эмодзи, заголовок) для подписи/текста
BLOCKS = {
    pgconn.PRIORITY_HIGH: ("🔥", "Нужно от тебя"),
    pgconn.PRIORITY_MED: ("📋", "Дела"),
    pgconn.PRIORITY_LOW: ("📊", "Сводка"),
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
                "SELECT id, priority, text, link, category FROM notifications "
                "WHERE account=%s AND sent_at IS NULL ORDER BY priority, created_at",
                (pgconn.get_account(),))
            return cur.fetchall()
    finally:
        conn.close()


def mark_sent(ids) -> None:
    if not ids:
        return
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE notifications SET sent_at=now() WHERE account=%s AND id=ANY(%s)",
                        (pgconn.get_account(), ids))
        conn.commit()
    finally:
        conn.close()


def _funnel_data(account):
    """Воронка из public.negotiations (заполняется ежедневным funnel-джобом)."""
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT state, count(*) FROM negotiations WHERE account=%s GROUP BY state",
                        (account,))
            by_state = {s: n for s, n in cur.fetchall()}
            cur.execute("SELECT resume_id, state, count(*) FROM negotiations WHERE account=%s "
                        "GROUP BY resume_id, state", (account,))
            per = {}
            for rid, st, n in cur.fetchall():
                per.setdefault(rid, {})[st] = n
            cur.execute("SELECT id, title FROM resumes")
            titles = {i: t for i, t in cur.fetchall()}
    finally:
        conn.close()
    total = sum(by_state.values())

    def pct(x):
        return round(100 * x / total) if total else 0

    sob = sum(by_state.get(s, 0) for s in _SOB)
    funnel = {"total": total, "sob": sob,
              "resp": by_state.get("response", 0), "disc": by_state.get("discard", 0),
              "sob_pct": pct(sob), "resp_pct": pct(by_state.get("response", 0)),
              "disc_pct": pct(by_state.get("discard", 0))}
    resumes = []
    for rid, d in per.items():
        tot = sum(d.values())
        s = sum(d.get(x, 0) for x in _SOB)
        resumes.append({"title": (titles.get(rid) or "Без резюме")[:46], "sob": s,
                        "total": tot, "pct": round(100 * s / tot) if tot else 0})
    resumes.sort(key=lambda r: -r["sob"])
    return funnel, resumes


async def _today_live(cfg) -> dict:
    """Сегодняшние просмотры/приглашения из hh /me (best-effort)."""
    tok = cfg.get("token") or {}
    out = {"views": 0, "invites": 0}
    if not tok.get("access_token"):
        return out
    api = ApiClient(access_token=tok["access_token"], refresh_token=tok.get("refresh_token", ""),
                    access_expires_at=tok.get("access_expires_at", 0),
                    user_agent=generate_android_useragent())
    try:
        c = (await api.get("/me")).get("counters", {})
        out["views"] = int(c.get("new_resume_views", 0) or 0)
        out["invites"] = int(c.get("unread_negotiations", 0) or 0)
    except Exception:
        pass
    finally:
        await api.aclose()
    return out


async def gather_card(cfg, account, has_problem) -> dict:
    today = dt.date.today()
    apps = pgconn.get_setting("_applications_count") or 0
    dat = pgconn.get_setting("_applications_date")
    apps = int(apps) if (str(apps).isdigit() and dat == today.isoformat()) else 0
    live = await _today_live(cfg)
    funnel, resumes = _funnel_data(account)
    return {
        "who": _user_label(),
        "date": f"{today.day} {_MONTHS[today.month]}",
        "status": "✅ Бот работает штатно" if not has_problem
                  else "⚠️ Есть проблема — детали в сообщении выше",
        "today": {"apps": apps, "views": live["views"], "invites": live["invites"]},
        "funnel": funnel, "resumes": resumes,
    }


def build_caption(action_rows) -> str:
    """HTML-подпись к фото — кликабельные действия (HIGH/MED)."""
    if not action_rows:
        return "✅ Срочных дел нет — всё под контролем."
    parts, last = [], None
    for _id, prio, text, link, _cat in action_rows:
        if prio != last:
            emoji, title = BLOCKS.get(prio, ("•", "Прочее"))
            parts.append(f"<b>{emoji} {title}</b>")
            last = prio
        line = "• " + html.escape(text)
        if link:
            line += f' <a href="{html.escape(link)}">открыть →</a>'
        parts.append(line)
    return "\n".join(parts)[:1000]


def build_text(rows) -> str:
    """Чистый текстовый фолбэк (если рендер карточки не удался)."""
    parts, last = [f"<b>✨ {html.escape(_user_label())}</b>"], None
    for _id, prio, text, link, _cat in rows:
        if prio != last:
            emoji, title = BLOCKS.get(prio, ("•", "Прочее"))
            parts.append(f"\n<b>{emoji} {title}</b>")
            last = prio
        line = html.escape(text)
        if link:
            line += f'\n<a href="{html.escape(link)}">открыть →</a>'
        parts.append(line)
    return "\n".join(parts)


async def main() -> None:
    if not pgconn.feature_enabled("notify"):
        print("feat.notify выключен — дайджест пропущен")
        return
    rows = fetch_unsent()
    if not rows:
        print("дайджест: нет новых уведомлений")
        return

    cfg = pgconn.app_config()
    account = pgconn.get_account()
    low = [r for r in rows if r[1] == pgconn.PRIORITY_LOW]
    actions = [r for r in rows if r[1] != pgconn.PRIORITY_LOW]
    has_problem = any(r[4] == "monitor" for r in actions)
    ids = [r[0] for r in rows]

    # Есть ежедневная сводка (heartbeat/funnel) -> рендерим красивую карточку.
    png = await digest_card.render_png(await gather_card(cfg, account, has_problem)) if low else None

    if DRY:
        print("DRY:", ("карточка PNG %d байт" % len(png)) if png else "текст-фолбэк")
        print(build_caption(actions) if png else build_text(rows))
        return

    tg = cfg.get("telegram") or {}
    token, dm = tg.get("token"), cfg.get("tg_user_id")
    if not (token and dm):
        print("Пользователь не привязан (/link) или нет токена — дайджест в очереди.")
        return

    bot = Bot(token, default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True))
    ok = True
    try:
        if png:
            try:
                await bot.send_photo(dm, BufferedInputFile(png, filename="digest.png"),
                                     caption=build_caption(actions))
            except Exception as e:
                print("TG photo error:", repr(e)[:160])
                ok = False
        else:
            text = build_text(rows)
            for i in range(0, len(text), 3800):
                try:
                    await bot.send_message(dm, text[i:i + 3800])
                except Exception as e:
                    print("TG error:", repr(e)[:160])
                    ok = False
                await asyncio.sleep(0.3)
    finally:
        await bot.session.close()

    if ok:
        mark_sent(ids)
        print(f"дайджест отправлен ({'фото' if png else 'текст'}, {len(rows)} уведомл.).")
    else:
        print("дайджест НЕ отправлен (ошибка TG) — останется в очереди.")


if __name__ == "__main__":
    asyncio.run(main())
