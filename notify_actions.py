"""
Сканирует диалоги с работодателями на hh.ru, извлекает "дела" (тест, интервью,
анкета, написать в ТГ, и т.п.), шлёт НОВЫЕ в Telegram + пишет в PG (action_items).
Состояние (config/seen/action_items) — в Postgres (схема из HH_DB_SCHEMA).

Запуск:  python notify_actions.py [--dry]
"""
import asyncio
import datetime as dt
import os
import sys

import httpx

from hh_applicant_tool.ai import ChatOpenAI
from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn

DRY = "--dry" in sys.argv

SYS = (
    "Ты анализируешь ПОСЛЕДНЕЕ сообщение работодателя в чате на hh.ru. "
    "Определи, требует ли оно от кандидата действия, которое нужно сделать ВНЕ этого чата "
    "(его нельзя выполнить, просто написав текстовый ответ в переписке). "
    "Считаются такими действиями: пройти тест/тестовое задание; пройти интервью с ботом-рекрутёром по ссылке; "
    "заполнить анкету/опрос/форму по ссылке или во внешнем боте; прийти или созвониться на собеседование "
    "(особенно если указаны дата/время/место); написать или связаться в другом мессенджере (Telegram, WhatsApp) "
    "или по телефону; зарегистрироваться на платформе/сайте; прислать документы или файлы. "
    "Если такое действие ТРЕБУЕТСЯ — верни ОДНУ короткую строку на русском в формате: "
    "<что сделать>[ | срок: <если есть>][ | контакт/ссылка: <если есть>]. "
    "Если работодатель просто задаёт вопрос, на который можно ответить ТЕКСТОМ прямо в этом чате "
    "(про опыт, навыки, формат работы, зарплатные ожидания и т.п.), либо это благодарность за отклик, "
    "«рассмотрим ваше резюме», отказ или общие вежливые фразы — верни РОВНО: НЕТ"
)


def _user_label():
    try:
        name = pgconn.get_setting("user.full_name")
    except Exception:
        name = None
    return name or os.environ.get("HH_DB_SCHEMA", "public")


async def tg_send(token, chat_id, text, topic_id=None):
    async with httpx.AsyncClient(timeout=25) as client:
        for i in range(0, len(text), 3800):
            chunk = text[i:i + 3800]
            data = {"chat_id": chat_id, "text": chunk,
                    "disable_web_page_preview": True}
            if topic_id:
                data["message_thread_id"] = topic_id
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data,
            )
            if r.status_code != 200:
                print("TG error:", r.status_code, r.text[:200])
            await asyncio.sleep(0.5)


async def main():
    cfg = pgconn.app_config()
    tok = cfg["token"]
    oa = cfg["openai"]
    tg = cfg.get("telegram") or {}

    api = ApiClient(
        access_token=tok["access_token"],
        refresh_token=tok["refresh_token"],
        access_expires_at=tok["access_expires_at"],
        user_agent=generate_android_useragent(),
        refresh_hook=pgconn.locked_token_refresh,
    )
    chat = ChatOpenAI(
        token=oa["token"], model=oa.get("model"),
        completion_endpoint=oa.get("completion_endpoint"),
        system_prompt=SYS, temperature=0.2, max_completion_tokens=160,
    )

    seen = pgconn.seen_keys("actions")
    fresh_seen = []
    new_items = []
    page = 0
    scanned = 0
    try:
        while True:
            r = await api.get(
                "/negotiations", page=page, per_page=100, status="active"
            )
            items = r.get("items", [])
            if not items:
                break
            for n in items:
                if n.get("state", {}).get("id") == "discard":
                    continue
                nid = n["id"]
                v = n.get("vacancy") or {}
                try:
                    m = await api.get(f"/negotiations/{nid}/messages", page=0)
                except Exception:
                    continue
                msgs = [x for x in (m.get("items") or []) if x.get("text")]
                emp = [
                    x for x in msgs
                    if x["author"]["participant_type"] == "employer"
                ]
                if not emp:
                    continue
                last = emp[-1]
                key = f"{nid}:{last.get('id')}"
                if key in seen:
                    continue
                scanned += 1
                q = (
                    f"Вакансия: {v.get('name','')}\n"
                    f"Сообщение работодателя:\n{last['text']}"
                )
                try:
                    ans = (await chat.send_message(q)).strip()
                except Exception as e:
                    print("LLM error:", repr(e)[:120])
                    continue
                fresh_seen.append(key)
                if ans.upper().startswith("НЕТ") or len(ans) < 3:
                    continue
                chat_id = n.get("chat_id") or nid
                new_items.append({
                    "vacancy": v.get("name", ""),
                    "chat_url": f"https://hh.ru/chat/{chat_id}",
                    "vacancy_url": v.get("alternate_url", ""),
                    "action": ans,
                    "nid": nid,
                    "chat_id": chat_id,
                })
            if page + 1 >= r.get("pages", 0):
                break
            page += 1
    finally:
        await api.aclose()

    print(f"scanned new employer messages: {scanned} | actions found: {len(new_items)}")
    for it in new_items:
        print(f"  • {it['vacancy']} :: {it['action']}")

    if DRY:
        print("DRY — ничего не отправлено и не сохранено.")
        return
    if not new_items:
        if fresh_seen:
            pgconn.add_seen("actions", fresh_seen)
        print("Новых дел нет.")
        return

    pgconn.add_action_items(new_items)

    if tg.get("token") and tg.get("chat_id"):
        lines = [
            f"👤 {_user_label()}",
            f"📋 Новые дела из диалогов hh ({len(new_items)}):",
            "",
        ]
        for i, it in enumerate(new_items, 1):
            lines.append(
                f"{i}. {it['vacancy']}\n"
                f"   → {it['action']}\n"
                f"   💬 диалог: {it['chat_url']}"
            )
        await tg_send(
            tg["token"], tg["chat_id"], "\n\n".join(lines),
            topic_id=tg.get("topic_id"),
        )
        print("Отправлено в Telegram.")
    else:
        print("Telegram не настроен.")

    pgconn.add_seen("actions", fresh_seen)


if __name__ == "__main__":
    asyncio.run(main())
