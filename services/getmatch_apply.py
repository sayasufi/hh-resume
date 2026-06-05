"""Авто-отклик и статусы GetMatch через API (getmatch.ru). Клиент — services/getmatch_api.py.

Отклик: GET /api/offers (exclude_applied) → POST /api/offers/{id}/apply. Дедуп seen('getmatch'),
дневной лимит, человеческие паузы. Сопроводительное письмо генерируется LLM (резюме кандидата +
вакансия) и шлётся при каждом отклике; обяз.-письмо без LLM пропускаем.
Статусы: после откликов синхронизируем все наши отклики (GET /api/applications/candidate) в таблицу
getmatch_apps (status/status_readable/reject_reason/company) — это источник для кабинета.

Гейт: feat.getmatch + привязка (getmatch.session ИЛИ tg_user_session). Telegram нужен только для
авто-релогина при истечении сессии; без него — перепривязка логином+кодом в кабинете.
Запуск: python /app/services/getmatch_apply.py [--dry].
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


LETTER_SYS = (
    "Ты пишешь короткое сопроводительное письмо на русском от первого лица для отклика на "
    "IT-вакансию. 3-5 предложений, по делу, без воды, клише и плейсхолдеров; не упоминай, что ты "
    "ИИ. Опирайся только на факты резюме. Без темы и заголовка — только текст письма."
)


def _letter_llm(cfg):
    """LLM для сопроводительных писем (как в apply_tests). None если LLM не настроена."""
    oa = cfg.get("openai") or {}
    if not oa.get("token"):
        return None
    from hh_applicant_tool.ai import ChatOpenAI
    resume = (cfg.get("resume_text") or "").strip()
    sysp = LETTER_SYS + (("\n\nРезюме:\n" + resume) if resume else "")
    return ChatOpenAI(token=oa["token"], model=oa.get("model"),
                      completion_endpoint=oa.get("completion_endpoint"),
                      system_prompt=sysp, temperature=0.5, max_completion_tokens=300)


async def _gen_letter(llm, offer) -> str:
    """Сгенерировать сопроводительное под вакансию (пусто при сбое/без LLM)."""
    if not llm:
        return ""
    pos = offer.get("position") or ""
    comp = (offer.get("company") or {}).get("name") or ""
    try:
        t = (await llm.send_message(
            f"Вакансия: «{pos}»" + (f" в {comp}" if comp else "") +
            ". Напиши сопроводительное письмо.")).strip()
        return t if len(t) >= 20 else ""
    except Exception as e:
        print(f"getmatch: письмо не сгенерировано: {repr(e)[:60]}")
        return ""


async def run():
    account = pgconn.get_account()
    if not pgconn.feature_enabled("getmatch"):
        print("getmatch: feat выключен — пропуск"); return
    cfg = pgconn.app_config()
    enc = cfg.get("tg_user_session")
    has_session = bool(pgconn.get_setting("getmatch.session", account=account))
    if not enc and not has_session:
        print("getmatch: не привязан (нет сессии и нет Telegram) — пропуск"); return

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

        letter_llm = _letter_llm(cfg)
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
                pos = (o.get("position") or "")[:42]
                letter = await _gen_letter(letter_llm, o)
                if o.get("cover_letter_required") and not letter:
                    print(f"getmatch: vac {vid} требует письмо, LLM недоступна — пропуск")
                    continue
                if DRY:
                    print(f"getmatch[dry]: откликнулся бы на {pos} (vac {vid}); письмо={len(letter)} симв.")
                    seen.add(vid); applied += 1
                    continue
                try:
                    r = await api.apply(o, me, cover_letter=letter)
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
