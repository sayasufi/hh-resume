"""
Сканирует диалоги с работодателями на hh.ru, извлекает "дела" (действия,
которые требуются от кандидата: тест, интервью, анкета, написать в ТГ, и т.п.)
и шлёт НОВЫЕ в Telegram + пишет в config/action_items.md.

Запуск:  python notify_actions.py [--dry]
  --dry  — только показать, ничего не слать и не сохранять в "seen".

Никаких сообщений работодателям НЕ отправляет (только GET по hh API).
"""
import sys
import json
import time
import datetime as dt

import requests

from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.ai import ChatOpenAI

DRY = "--dry" in sys.argv
CFG = "/app/config/config.json"
SEEN_PATH = "/app/config/actions_seen.json"
MD_PATH = "/app/config/action_items.md"

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


def tg_send(token, chat_id, text):
    for i in range(0, len(text), 3800):
        chunk = text[i:i + 3800]
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
            timeout=25,
        )
        if not r.ok:
            print("TG error:", r.status_code, r.text[:200])
        time.sleep(0.5)


def main():
    with open(CFG, encoding="utf-8") as f:
        cfg = json.load(f)
    tok = cfg["token"]
    oa = cfg["openai"]
    tg = cfg.get("telegram") or {}

    api = ApiClient(
        access_token=tok["access_token"],
        refresh_token=tok["refresh_token"],
        access_expires_at=tok["access_expires_at"],
        user_agent=generate_android_useragent(),
    )
    chat = ChatOpenAI(
        token=oa["token"], model=oa.get("model"),
        completion_endpoint=oa.get("completion_endpoint"),
        system_prompt=SYS, temperature=0.2, max_completion_tokens=160,
    )

    try:
        with open(SEEN_PATH, encoding="utf-8") as f:
            seen = set(json.load(f))
    except Exception:
        seen = set()

    new_items = []
    page = 0
    scanned = 0
    while True:
        r = api.get("/negotiations", page=page, per_page=100, status="active")
        items = r.get("items", [])
        if not items:
            break
        for n in items:
            if n.get("state", {}).get("id") == "discard":
                continue
            nid = n["id"]
            v = n.get("vacancy") or {}
            try:
                m = api.get(f"/negotiations/{nid}/messages", page=0)
            except Exception:
                continue
            msgs = [x for x in (m.get("items") or []) if x.get("text")]
            # последнее сообщение ИМЕННО от работодателя (не зависит от того,
            # ответил ли уже бот после него)
            emp = [x for x in msgs if x["author"]["participant_type"] == "employer"]
            if not emp:
                continue
            last = emp[-1]
            key = f"{nid}:{last.get('id')}"
            if key in seen:
                continue
            scanned += 1
            q = f"Вакансия: {v.get('name','')}\nСообщение работодателя:\n{last['text']}"
            try:
                ans = chat.send_message(q).strip()
            except Exception as e:
                print("LLM error:", repr(e)[:120])
                continue
            seen.add(key)  # помечаем рассмотренным в любом случае
            if ans.upper().startswith("НЕТ") or len(ans) < 3:
                continue
            chat_id = n.get("chat_id") or nid  # ссылка на чат использует chat_id, а не id отклика
            new_items.append({
                "vacancy": v.get("name", ""),
                "chat_url": f"https://hh.ru/chat/{chat_id}",
                "vacancy_url": v.get("alternate_url", ""),
                "action": ans,
                "nid": nid,
                "ts": dt.datetime.now().isoformat(timespec="seconds"),
            })
        if page + 1 >= r.get("pages", 0):
            break
        page += 1

    print(f"scanned new employer messages: {scanned} | actions found: {len(new_items)}")
    for it in new_items:
        print(f"  • {it['vacancy']} :: {it['action']}")

    if DRY:
        print("DRY — ничего не отправлено и не сохранено.")
        return
    if not new_items:
        print("Новых дел нет.")
        return

    # файл (человекочитаемый)
    with open(MD_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n## Дела на {dt.datetime.now():%Y-%m-%d %H:%M}\n")
        for it in new_items:
            f.write(
                f"- [ ] **{it['vacancy']}** — {it['action']}  \n"
                f"  💬 диалог: {it['chat_url']}  \n"
                f"  📄 вакансия: {it['vacancy_url']}\n"
            )

    # telegram
    if tg.get("token") and tg.get("chat_id"):
        lines = [f"📋 Новые дела из диалогов hh ({len(new_items)}):", ""]
        for i, it in enumerate(new_items, 1):
            lines.append(
                f"{i}. {it['vacancy']}\n"
                f"   → {it['action']}\n"
                f"   💬 диалог: {it['chat_url']}"
            )
        tg_send(tg["token"], tg["chat_id"], "\n\n".join(lines))
        print("Отправлено в Telegram.")
    else:
        print("Telegram не настроен — только файл.")

    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f)


if __name__ == "__main__":
    main()
