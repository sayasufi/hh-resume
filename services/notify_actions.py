"""Сканирует диалоги с работодателями, LLM-классифицирует внешние «дела» и кладёт
их в очередь уведомлений (pgconn.notify) с приоритетом. Отправит потом send_digest.

Категория -> приоритет:
  contact (написать/позвонить в TG/мессенджер/телефон, дать контакт) -> 🔴 HIGH
  form    (анкета/опрос/форма/регистрация/внешний бот-рекрутёр)       -> 🟡 MED
  test    (тест/тестовое задание — бот делает сам)                    -> 🟢 LOW
  interview (приглашение на интервью/созвон) -> пропуск (владеет reply_employers/handoff)
  none -> пропуск (обычный вопрос — на него ответит reply_employers)

Запуск:  python notify_actions.py [--dry]
"""
import asyncio
import re
import sys

from hh_applicant_tool.ai import ChatOpenAI
from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn

DRY = "--dry" in sys.argv

SYS = (
    "Ты анализируешь ПОСЛЕДНЕЕ сообщение работодателя в чате на hh.ru. Определи, "
    "требует ли оно действия кандидата ВНЕ этого чата, и к какой категории относится. "
    "Ответь СТРОГО в формате: `<категория> | <что сделать одной строкой на русском>`.\n"
    "Категории:\n"
    "- contact — нужно написать/позвонить работодателю в Telegram/WhatsApp/по телефону "
    "или оставить свой контакт;\n"
    "- form — пройти анкету/опрос/форму по ссылке, зарегистрироваться на платформе, "
    "ИЛИ пройти автоматический скрининг/первичное интервью с ботом-рекрутёром или ПО "
    "ССЫЛКЕ (ГигаРекрутер, Telegram-бот) — это НЕ живой разговор;\n"
    "- test — пройти тест/тестовое задание;\n"
    "- interview — ЖИВОЙ человек (сотрудник) зовёт на собеседование/созвон/встречу или "
    "предлагает конкретное время для звонка/встречи с человеком (без бота и без ссылки);\n"
    "- none — ничего из перечисленного (обычный вопрос, на который можно ответить в "
    "чате, благодарность, «рассмотрим резюме», отказ).\n"
    "Если категория none — ответь просто: none"
)

PRIO = {
    "contact": pgconn.PRIORITY_HIGH,
    "form": pgconn.PRIORITY_MED,
    "test": pgconn.PRIORITY_LOW,
}

# Приглашение ГигаРекрутера / бота-рекрутёра. Если ГР активен (feat.giga + tg-сессия) —
# бот проходит интервью сам, поэтому в «дела» пользователю это НЕ кладём. Если ГР
# выключен/не подключён — кладём как обычное дело (пользователь проходит вручную).
GR_MARK_RE = re.compile(
    r"giga_recruiter_bot|t\.me/\S*bot\?start=|бот[- ]?рекрут|"
    r"первичн\w* интервью\s+с\s+ботом|автоматическ\w*\s+(?:скрининг|интервью)", re.I)

URL_RE = re.compile(r"https?://\S+")  # первая ссылка из сообщения -> action_url дела


def _norm_cat(raw: str):
    """Нормализуем категорию из ответа LLM (бэктики/кавычки/префикс «категория:»/синонимы).
    Возвращает contact|form|test|interview|none, либо None если не распознали — тогда
    дело НЕ помечается seen и переразберётся в следующий ран (а не теряется молча)."""
    s = (raw or "").strip().strip("`'\"*•- ").lower()
    if ":" in s:                       # «категория: form» -> form
        s = s.split(":")[-1].strip()
    s = s.strip("`'\"*•-. ").lower()
    if not s:
        return None
    if s.startswith(("contact", "контакт")):
        return "contact"
    if s.startswith(("form", "анкет", "форм", "опрос", "регистр")):
        return "form"
    if s.startswith(("test", "тест")):
        return "test"
    if s.startswith(("interview", "интервью", "собес", "созвон")):
        return "interview"
    if s.startswith(("none", "нет", "ничего", "no", "—")):
        return "none"
    return None


async def main():
    if not pgconn.feature_enabled("notify"):
        print("feat.notify выключен в Mini App — пропуск notify_actions")
        return
    cfg = pgconn.app_config()
    tok = cfg["token"]
    oa = cfg["openai"]
    # ГР активен -> приглашения ГигаРекрутера проходит бот сам, в «дела» не кладём
    giga_active = pgconn.feature_enabled("giga") and bool(cfg.get("tg_user_session"))

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
    handoff = pgconn.seen_keys("handoff")  # чаты, переданные тебе (интервью) — мимо
    fresh_seen = []
    queued = []  # (prio, task, link, dedup, nid, chat_id, vacancy)
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
                if str(nid) in handoff:
                    continue
                v = n.get("vacancy") or {}
                try:
                    m = await api.get(f"/negotiations/{nid}/messages", page=0)
                    _pages = m.get("pages", 1)
                    if _pages > 1:   # последняя страница — там СВЕЖЕЕ сообщение работодателя
                        m = await api.get(f"/negotiations/{nid}/messages", page=_pages - 1)
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
                if giga_active and GR_MARK_RE.search(last.get("text") or ""):
                    fresh_seen.append(key)  # ГР-приглашение: бот пройдёт сам, юзера не дёргаем
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
                cat_raw, _, task = ans.partition("|")
                cat = _norm_cat(cat_raw)
                task = task.strip()
                if cat is None:   # не распознали -> НЕ помечаем seen, переразберём в след. ран
                    print("notify: неразборчивая категория LLM, не помечаю seen:", ans[:90])
                    continue
                fresh_seen.append(key)  # терминальная классификация -> больше не дёргаем
                prio = PRIO.get(cat)
                if not prio or len(task) < 3:
                    continue  # none/interview -> пропуск (seen уже стоит)
                chat_id = n.get("chat_id") or nid
                _u = URL_RE.search(last.get("text") or "")
                action_url = _u.group(0).rstrip(").,;") if _u else ""
                queued.append((
                    prio, task, f"https://hh.ru/chat/{chat_id}",
                    f"action:{key}", nid, chat_id, v.get("name", ""), action_url,
                ))
            if page + 1 >= r.get("pages", 0):
                break
            page += 1
    finally:
        await api.aclose()

    print(f"scanned: {scanned} | дел в очередь: {len(queued)}")
    for prio, task, link, *_ in queued:
        print(f"  [P{prio}] {task} :: {link}")

    if DRY:
        print("DRY — ничего не сохранено и не поставлено в очередь.")
        return

    for prio, task, link, dedup, nid, chat_id, vac, action_url in queued:
        pgconn.notify(
            prio, f"{task} — {vac}" if vac else task,
            category="action", link=link, dedup_key=dedup,
        )
    if queued:
        pgconn.add_action_items([
            {
                "nid": nid, "chat_id": chat_id, "vacancy": vac,
                "action": task, "chat_url": link, "vacancy_url": "",
                "action_url": action_url,
            }
            for prio, task, link, dedup, nid, chat_id, vac, action_url in queued
        ])
    if fresh_seen:
        pgconn.add_seen("actions", fresh_seen)
    print("готово.")


if __name__ == "__main__":
    asyncio.run(main())
