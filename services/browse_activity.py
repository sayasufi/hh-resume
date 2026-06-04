"""Человекоподобная активность на hh: умный просмотр релевантных вакансий.

Зачем: попасть в список «кто смотрел вакансию» у работодателя (может подтолкнуть
открыть резюме) + аккаунт выглядит живым. Честно: эффект слабый (аккаунт и так
активен), но безопасный. НЕ откликается и не пишет — только GET-просмотр.

Человекоподобность: смотрит в основном сверху списка (релевантные), ниже — реже;
разная глубина чтения (чаще беглый взгляд, иногда вдумчиво); иногда листает 2-ю
страницу; иногда открывает страницу работодателя; иногда заходит в свои отклики.

Запуск: python browse_activity.py [--dry]   (обычно через run_all)
"""
import asyncio
import random
import sys

from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn

DRY = "--dry" in sys.argv


async def _dwell():
    """Пауза «чтения»: чаще беглый взгляд, иногда вдумчивое чтение."""
    if random.random() < 0.3:
        await asyncio.sleep(random.uniform(15, 40))
    else:
        await asyncio.sleep(random.uniform(3, 10))


async def main():
    if not pgconn.feature_enabled("browse"):
        print("feat.browse выключен в Mini App — пропуск browse_activity")
        return
    cfg = pgconn.app_config()
    tok = cfg.get("token") or {}
    if not tok.get("access_token"):
        print("browse: нет токена")
        return

    api = ApiClient(
        access_token=tok["access_token"],
        refresh_token=tok["refresh_token"],
        access_expires_at=tok.get("access_expires_at", 0),
        user_agent=generate_android_useragent(),
        refresh_hook=pgconn.locked_token_refresh,
    )
    resume_id = pgconn.get_setting("apply.resume_id")
    viewed = 0
    employers = 0
    try:
        await api.get("/me")  # «открыли приложение»

        # Соберём вакансии (иногда листаем 2-ю страницу, как живой скролл).
        collected = []  # (vacancy_id, employer_id)
        if resume_id:
            pages = 2 if random.random() < 0.4 else 1
            for page in range(pages):
                try:
                    r = await api.get(
                        f"/resumes/{resume_id}/similar_vacancies",
                        page=page, per_page=50,
                    )
                except Exception as e:
                    print("browse: не получил вакансии:", repr(e)[:60])
                    break
                items = r.get("items", [])
                if not items:
                    break
                for v in items:
                    if v.get("archived"):
                        continue
                    collected.append(
                        (v["id"], (v.get("employer") or {}).get("id"))
                    )
                if not DRY and page + 1 < pages:
                    await asyncio.sleep(random.uniform(2, 6))  # «проскроллил»

        # Человек смотрит в основном верхние (релевантные), ниже — всё реже.
        budget = random.randint(4, 14)
        plan = []
        for i, item in enumerate(collected):
            if random.random() < max(0.2, 1.0 - i * 0.04):
                plan.append(item)
            if len(plan) >= budget:
                break

        seen_emp = set()
        for vid, emp in plan:
            if DRY:
                print(f"DRY: смотрел бы вакансию {vid}")
                viewed += 1
                continue
            try:
                await api.get(f"/vacancies/{vid}")
                viewed += 1
                pgconn.bump_activity("browse", 1)
            except Exception:
                pass
            await _dwell()
            # иногда заглянуть на страницу работодателя (проверить компанию)
            if emp and emp not in seen_emp and random.random() < 0.25:
                try:
                    await api.get(f"/employers/{emp}")
                    seen_emp.add(emp)
                    employers += 1
                    await asyncio.sleep(random.uniform(2, 8))
                except Exception:
                    pass

        # иногда проверить свои резюме/отклики (как живой кандидат)
        if not DRY:
            if random.random() < 0.5:
                try:
                    await api.get("/resumes/mine")
                except Exception:
                    pass
            if random.random() < 0.4:
                try:
                    await api.get("/negotiations", per_page=20)
                except Exception:
                    pass
    finally:
        await api.aclose()

    print(f"browse: вакансий {viewed}, работодателей {employers}")


if __name__ == "__main__":
    asyncio.run(main())
