"""Лёгкий «онлайн»-пинг: держит аккаунт «в сети / был только что» для рекрутёров.
Только GET /me (+ иногда своё резюме), без тяжёлого браузинга. Per-account через
оркестрацию (feature=browse), частый cron. Рандомизирован, чтобы не быть clockwork."""
import asyncio
import random

from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn


async def main():
    if not pgconn.feature_enabled("browse"):
        return
    cfg = pgconn.app_config()
    tok = cfg.get("token") or {}
    if not tok.get("access_token"):
        return
    api = ApiClient(
        access_token=tok["access_token"],
        refresh_token=tok.get("refresh_token"),
        access_expires_at=tok.get("access_expires_at", 0),
        user_agent=generate_android_useragent(),
        refresh_hook=pgconn.locked_token_refresh,
    )
    try:
        await asyncio.sleep(random.uniform(0, 75))  # рассинхрон от ровной минуты
        await api.get("/me")  # «зашёл на hh» -> онлайн
        if random.random() < 0.25:
            await asyncio.sleep(random.uniform(2, 8))
            await api.get("/resumes/mine")  # глянул своё резюме
    except Exception as e:
        print("online-ping:", type(e).__name__)
    finally:
        await api.aclose()


if __name__ == "__main__":
    asyncio.run(main())
