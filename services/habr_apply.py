"""Habr Career — операция отклика: профильные вакансии -> сопроводительное (LLM) ->
отклик -> дедуп/лимит/учёт. Гейт: feat.habr + habr.session. Структура как getmatch_apply.

Запуск: python /app/services/habr_apply.py [--dry]   (обычно через Prefect JOB).
"""
import asyncio
import random
import sys

import psycopg

import habr_api
from hh_applicant_tool.ai import ChatOpenAI
from hh_applicant_tool.storage import pgconn

DRY = "--dry" in sys.argv
DEFAULT_MAX = 15

LETTER_SYS = (
    "Ты — кандидат, пишешь короткое сопроводительное к отклику на вакансию на Хабр Карьере. "
    "3-4 предложения, живой человеческий язык, без канцелярита и буззвордов. Строго по опыту "
    "ниже — не выдумывай навыков, которых нет. Скажи, что заинтересовала вакансия, 1-2 фразы "
    "почему подходишь (конкретный релевантный опыт), и что готов обсудить. Без markdown, без "
    "слова «резюме».\n\n=== ОПЫТ ===\n{resume}\n=== КОНЕЦ ===")


async def _gen_letter(oa, resume, title, company):
    if not (oa and oa.get("token") and resume):
        return ""
    try:
        chat = ChatOpenAI(token=oa["token"], model=oa.get("model"),
                          completion_endpoint=oa.get("completion_endpoint"),
                          system_prompt=LETTER_SYS.format(resume=resume[:3000]),
                          temperature=0.5, max_completion_tokens=300)
        t = ((await chat.send_message(f"Вакансия «{title}» в компании «{company}». Напиши сопроводительное.")) or "").strip()
    except Exception as e:
        print(f"habr: письмо не сгенерилось ({type(e).__name__}) — отклик без письма")
        return ""
    if len(t) < 20:
        return ""
    return t


def _query(cfg, account):
    """Поисковый запрос: настройка habr.query, иначе из заголовка hh-резюме."""
    q = pgconn.get_setting("habr.query", account=account)
    if q:
        return q
    title = ((cfg.get("resume") or {}).get("title") or "").strip()
    return title or "python"


async def run():
    account = pgconn.get_account()
    if not pgconn.feature_enabled("habr"):
        print("habr: feat выключен — пропуск")
        return
    if not pgconn.get_setting("habr.session", account=account):
        print("habr: нет сессии (нужен логин) — пропуск")
        return

    lock_conn = pgconn.connect()
    api = None
    locked = False
    try:
        with lock_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (f"habr:{account}",))
            if not cur.fetchone()[0]:
                print("habr: уже выполняется — пропуск")
                return
        locked = True

        cfg = pgconn.app_config()
        api = habr_api.HabrAPI(account)
        try:
            await api.ensure_auth()
        except habr_api.HabrError as e:
            print(f"habr: логин не удался: {e}")
            if DRY:
                return
            pgconn.notify(pgconn.PRIORITY_MED,
                          f"Не удалось войти в Habr Career — проверь логин/пароль/2captcha. ({e})",
                          category="action", dedup_key="habr:login")
            raise

        _lim = pgconn.get_setting("habr.max_per_day", DEFAULT_MAX)
        limit = DEFAULT_MAX if _lim is None else int(_lim)
        oa = cfg.get("openai")
        resume = (cfg.get("resume_text") or "").strip()
        query = _query(cfg, account)
        offers = await api.offers(q=query)
        print(f"habr: вошли, вакансий по «{query}»: {len(offers)}, лимит {limit}")

        seen = pgconn.seen_keys("habr")
        applied = 0
        for v in offers:
            if applied >= limit:
                break
            vid = str(v["id"])
            kind = (v.get("response") or {}).get("kind")
            if kind == "applied" or vid in seen:
                continue
            title = v.get("title", "")
            company = (v.get("company") or {}).get("title", "")
            cover = await _gen_letter(oa, resume, title, company)
            if DRY:
                print(f"habr[dry]: откликнулся бы на {title[:42]} (письмо={len(cover)} симв)")
                applied += 1
                continue
            r = await api.apply(v["id"], cover)
            if r.status_code == 200:
                pgconn.add_seen("habr", vid)
                seen.add(vid)
                pgconn.bump_activity("habr", 1, account=account)
                applied += 1
                print(f"habr: откликнулся на {title[:42]}")
            else:
                print(f"habr: отклик не прошёл на {vid} ({r.status_code}) — повторю позже")
            await asyncio.sleep(random.uniform(4, 12))
        print(f"habr: готово, откликов {applied}")
    finally:
        if api is not None:
            await api.aclose()
        if locked:
            with lock_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"habr:{account}",))
            lock_conn.commit()
        lock_conn.close()


if __name__ == "__main__":
    asyncio.run(run())
