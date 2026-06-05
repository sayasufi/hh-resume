"""Авто-отклик и статусы GetMatch через API (getmatch.ru). Клиент — services/getmatch_api.py.

Отклик: GET /api/offers (exclude_applied) → POST /api/offers/{id}/apply. Дедуп seen('getmatch'),
дневной лимит, человеческие паузы. Вакансии с обязательным сопроводительным письмом пока пропускаем.
Статусы: после откликов синхронизируем все наши отклики (GET /api/applications/candidate) в таблицу
getmatch_apps (status/status_readable/reject_reason/company) — это источник для кабинета.

Гейт: feat.getmatch + app_config.tg_user_session. Запуск: python /app/services/getmatch_apply.py [--dry].
"""
import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # /app для import services.*

from hh_applicant_tool.storage import pgconn
from services.getmatch_api import GetMatchAPI, GetMatchError, abs_url, profile_filters

DRY = "--dry" in sys.argv
SEEN_KIND = "getmatch"
DEFAULT_MAX = 50


def _today_count(account: str) -> int:
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(count,0) FROM activity_daily "
                        "WHERE account=%s AND kind=%s AND day=current_date", (account, SEEN_KIND))
            r = cur.fetchone()
        return int(r[0]) if r else 0
    finally:
        conn.close()


def _ensure_table(cur):
    cur.execute("CREATE TABLE IF NOT EXISTS getmatch_apps ("
                "account text NOT NULL, vacancy_id text NOT NULL, title text, url text, "
                "applied_at timestamptz DEFAULT now(), PRIMARY KEY (account, vacancy_id))")
    for col in ("status text", "status_readable text", "company text", "reject_reason text"):
        cur.execute(f"ALTER TABLE getmatch_apps ADD COLUMN IF NOT EXISTS {col}")


def _sync_statuses(account: str, apps: list) -> int:
    """Зеркалим отклики из API в getmatch_apps (статусы для кабинета). Возвращает число записей."""
    conn = pgconn.connect()
    n = 0
    try:
        with conn.cursor() as cur:
            _ensure_table(cur)
            for a in apps:
                v = a.get("vacancy") or {}
                vid = str(v.get("id") or "")
                if not vid:
                    continue
                cur.execute(
                    "INSERT INTO getmatch_apps(account,vacancy_id,title,url,company,status,"
                    "status_readable,reject_reason,applied_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(account,vacancy_id) DO UPDATE SET status=EXCLUDED.status, "
                    "status_readable=EXCLUDED.status_readable, reject_reason=EXCLUDED.reject_reason, "
                    "title=EXCLUDED.title, company=EXCLUDED.company, url=EXCLUDED.url",
                    (account, vid, v.get("position") or "", abs_url(v.get("url") or ""),
                     (v.get("company") or {}).get("name") or "",
                     a.get("status") or "", a.get("status_readable") or "",
                     a.get("reject_reason") or "", a.get("applied_at")))
                n += 1
        conn.commit()
    finally:
        conn.close()
    return n


async def run():
    account = pgconn.get_account()
    if not pgconn.feature_enabled("getmatch"):
        print("getmatch: feat выключен — пропуск"); return
    cfg = pgconn.app_config()
    enc = cfg.get("tg_user_session")
    if not enc:
        print("getmatch: нет tg_user_session — пропуск"); return

    lock_conn = pgconn.connect()
    with lock_conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (f"getmatch:{account}",))
        if not cur.fetchone()[0]:
            print("getmatch: уже выполняется — пропуск"); lock_conn.close(); return
    api = GetMatchAPI(account, enc)
    try:
        try:
            me = await api.ensure_auth()
        except GetMatchError as e:
            print(f"getmatch: логин не удался: {e}")
            if not DRY:
                pgconn.notify(pgconn.PRIORITY_MED,
                              "Не удалось войти в GetMatch по API — проверьте профиль и подключение "
                              f"Telegram. ({e})", category="action", dedup_key="getmatch:login")
            return
        print(f"getmatch: вошли как {me.get('first_name')} {me.get('last_name')}")

        limit = int(pgconn.get_setting("getmatch.max_per_day", DEFAULT_MAX) or DEFAULT_MAX)
        sent_today = _today_count(account)
        seen = pgconn.seen_keys(SEEN_KIND)
        applied = 0
        if sent_today >= limit:
            print(f"getmatch: дневной лимит достигнут ({sent_today}/{limit})")
        else:
            offers = await api.offers(limit=max(limit * 2, 40), **profile_filters(me))
            print(f"getmatch: вакансий-кандидатов: {len(offers)}")
            for o in offers:
                if sent_today + applied >= limit:
                    break
                vid = str(o.get("id") or "")
                if not vid or vid in seen:
                    continue
                if o.get("cover_letter_required"):
                    print(f"getmatch: vac {vid} требует сопроводительное — пропуск (v1)")
                    continue
                pos = (o.get("position") or "")[:42]
                if DRY:
                    print(f"getmatch[dry]: откликнулся бы на {pos} (vac {vid})")
                    seen.add(vid); applied += 1
                    continue
                try:
                    r = await api.apply(o, me)
                except Exception as e:
                    print(f"getmatch: apply vac {vid} ошибка: {repr(e)[:70]}")
                    continue
                pgconn.add_seen(SEEN_KIND, [vid]); seen.add(vid)
                if r.status_code == 200:
                    pgconn.bump_activity("getmatch", 1); applied += 1
                    print(f"getmatch: ✅ отклик {pos} (vac {vid}) [{sent_today + applied}/{limit}]")
                    await asyncio.sleep(random.uniform(4, 12))
                else:
                    print(f"getmatch: vac {vid} apply -> {r.status_code} {r.text[:60]}")
        print(f"getmatch: откликов за прогон: {applied}")

        # --- синхронизация статусов (всегда, даже в dry) ---
        try:
            apps = await api.applications(limit=200)
            n = _sync_statuses(account, apps)
            print(f"getmatch: статусы синхронизированы ({n} откликов)")
        except Exception as e:
            print(f"getmatch: синк статусов ошибка: {repr(e)[:70]}")
    finally:
        await api.aclose()
        with lock_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"getmatch:{account}",))
        lock_conn.close()


if __name__ == "__main__":
    asyncio.run(run())
