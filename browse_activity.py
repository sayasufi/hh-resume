"""Человекоподобная активность на hh: периодический просмотр релевантных вакансий.

Зачем: главный плюс — кандидат попадает в список «кто смотрел вакансию» у
работодателя (может подтолкнуть открыть резюме); плюс аккаунт выглядит живым/
онлайн. Честно: эффект слабый (аккаунт и так активен откликами+поднятием резюме),
но безопасный. НЕ откликается и не пишет — только GET-просмотр.

Человекоподобность: случайное число вакансий за сессию и случайные паузы «чтения»,
случайный порядок, иногда заход в свои резюме. Запускается несколько раз в день в
разное время (cron + случайный sleep).

Запуск: python browse_activity.py [--dry]   (обычно через run_all)
"""
import asyncio
import random
import sys

from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn

DRY = "--dry" in sys.argv


async def main():
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
    try:
        # «открыли приложение»
        await api.get("/me")

        # подобрали релевантные вакансии под резюме
        vac_ids = []
        if resume_id:
            try:
                r = await api.get(
                    f"/resumes/{resume_id}/similar_vacancies",
                    page=0, per_page=100,
                )
                vac_ids = [
                    v["id"] for v in r.get("items", [])
                    if not v.get("archived")
                ]
            except Exception as e:
                print("browse: не получил вакансии:", repr(e)[:60])

        random.shuffle(vac_ids)
        target = random.randint(5, 15)  # сколько «прочитать» за сессию
        for vid in vac_ids[:target]:
            if DRY:
                print(f"DRY: смотрел бы вакансию {vid}")
                viewed += 1
                continue
            try:
                await api.get(f"/vacancies/{vid}")
                viewed += 1
            except Exception:
                pass
            # человекоподобная пауза «чтения»
            await asyncio.sleep(random.uniform(3, 14))

        # иногда заглянуть в свои резюме (как живой человек проверяет отклик)
        if not DRY and random.random() < 0.5:
            try:
                await api.get("/resumes/mine")
            except Exception:
                pass
    finally:
        await api.aclose()

    print(f"browse: просмотрено вакансий {viewed} (цель {target if vac_ids else 0})")


if __name__ == "__main__":
    asyncio.run(main())
