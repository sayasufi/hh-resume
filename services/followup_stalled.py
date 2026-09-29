"""followup-stalled: ОДИН вежливый фоллоу-ап по зависшим откликам (state=response,
не отвечены работодателем > STALE_DAYS) — ре-энгейджмент + активность аккаунта.
Per-account (feature=reply). Один раз на отклик (дедуп seen_keys 'followup'),
лимит за прогон, только messaging_status=ok. НЕ спам: одно сообщение, не повторяем."""
import asyncio
import random
import sys
from datetime import datetime, timezone

from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn
from hh_applicant_tool.utils import dialog_rules as rules
from hh_applicant_tool.utils.date import parse_api_datetime

DRY = "--dry" in sys.argv  # показать, кому бы напомнили, ничего не отправляя и не помечая
STALE_DAYS = 7
MAX_PER_RUN = 6
TEMPLATES = (
    "Добрый день! Подскажите, пожалуйста, актуальна ли ещё вакансия «{vac}»? Буду рад обсудить детали.",
    "Здравствуйте! Откликался на «{vac}» — хотел уточнить, рассматриваете ли ещё кандидатов? Готов подробнее рассказать об опыте.",
    "Добрый день! Интересует статус по вакансии «{vac}». Если ещё актуально — с удовольствием обсужу детали в удобное время.",
    "Здравствуйте! Напомню о себе по отклику на «{vac}». Очень заинтересован в позиции, буду рад продолжить общение.",
)


async def main():
    if not pgconn.feature_enabled("reply"):
        print("feat.reply выкл — пропуск followup-stalled")
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
    seen = set(map(str, pgconn.seen_keys("followup")))
    # Не больше ОДНОГО фоллоуапа работодателю в день: раньше одному HR прилетало 5 разных
    # шаблонов подряд (по каждой вакансии) — очевидный бот.
    today = datetime.now().date().isoformat()
    emp_today = {k.split(":", 1)[0] for k in pgconn.seen_keys("followup_emp")
                 if k.endswith(":" + today)}
    now = datetime.now(timezone.utc)
    sent = 0
    try:
        for pg in range(12):
            r = await api.get("/negotiations", page=pg, per_page=100, status="all")
            for n in r.get("items", []):
                if sent >= MAX_PER_RUN:
                    break
                if (n.get("state") or {}).get("id") != "response":
                    continue
                if (n.get("messaging_status") or "ok") != "ok":
                    continue
                nid = n["id"]
                if str(nid) in seen:
                    continue
                try:
                    upd = parse_api_datetime(n["updated_at"]).astimezone(timezone.utc)
                except Exception:
                    continue
                if (now - upd).days < STALE_DAYS:
                    continue
                emp_id = str(((n.get("vacancy") or {}).get("employer") or {}).get("id") or "")
                if emp_id and emp_id in emp_today:
                    continue  # этому работодателю сегодня уже напоминали — вернёмся завтра
                # Если последнее сообщение в чате — от работодателя (HR/скринер ждёт наш ответ),
                # фоллоу-ап НЕ шлём: на вопрос должен ответить reply-employers по существу, а не
                # прилетать generic «актуальна ли ещё вакансия?». seen НЕ ставим — вернёмся позже.
                try:
                    _msgs = (await api.get(
                        f"/negotiations/{nid}/messages", per_page=100)).get("items", [])
                except Exception:
                    continue  # не прочитали чат — не шлём вслепую
                if _msgs and (_msgs[-1].get("author") or {}).get(
                        "participant_type") == "employer":
                    continue
                _emp = [m.get("text") or "" for m in _msgs
                        if (m.get("author") or {}).get("participant_type") == "employer"]
                _ours = [m.get("text") or "" for m in _msgs
                         if (m.get("author") or {}).get("participant_type") != "employer"]
                # Мяч на нашей стороне (просили написать в TG, анкету, ТЗ; или мы сами что-то
                # пообещали) — «актуальна ли вакансия?» тут неуместно. Не напоминаем вовсе.
                if rules.ball_on_our_side(_emp, _ours):
                    if DRY:
                        print("  мяч на нашей стороне — не напоминаем:", nid)
                    else:
                        pgconn.add_seen("followup", str(nid))
                    seen.add(str(nid))
                    continue
                vac = rules.short_title((n.get("vacancy") or {}).get("name") or "вашу вакансию")
                if DRY:
                    print("🧪 dry фоллоу-ап:", nid, "|", random.choice(TEMPLATES).format(vac=vac))
                    sent += 1
                    if emp_id:
                        emp_today.add(emp_id)
                    seen.add(str(nid))
                    continue
                try:
                    await api.post(
                        f"/negotiations/{nid}/messages",
                        message=random.choice(TEMPLATES).format(vac=vac),
                        delay=random.uniform(1, 4),
                    )
                    sent += 1
                    pgconn.bump_activity("followup", 1)
                    if emp_id:
                        pgconn.add_seen("followup_emp", f"{emp_id}:{today}")
                        emp_today.add(emp_id)
                    print("📨 Фоллоу-ап:", (n.get("vacancy") or {}).get("alternate_url", nid))
                except Exception as e:
                    print("followup", nid, type(e).__name__, str(e)[:70])
                # помечаем seen в любом случае (успех ИЛИ закрыто) — не долбим повторно
                pgconn.add_seen("followup", str(nid))
                seen.add(str(nid))
            if sent >= MAX_PER_RUN or pg + 1 >= r.get("pages", 0):
                break
        print("followup-stalled: отправлено", sent)
    finally:
        await api.aclose()


if __name__ == "__main__":
    asyncio.run(main())
