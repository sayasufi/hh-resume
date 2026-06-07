"""
services/tg_crawler.py — центральный краулер вакансий.
Читает broadcast-каналы из tg.catalog через t.me/s (HTTP, без флуда), LLM-парсит каждый
новый пост, сохраняет вакансии в tg_vacancies. Дедуп seen_keys('tg_crawl') по channel:post_id.
Группы-чаты (t.me/s их не отдаёт) краулятся отдельно joined-аккаунтом (фаза 2).
Env для теста: CRAWL_N (только первые N каналов), CRAWL_MAX (потолок LLM-вызовов).
Запуск: python /app/services/tg_crawler.py
"""
import sys, os, re, json, asyncio, html as _html
from datetime import datetime, timezone, timedelta
sys.path.insert(0, "/app")
from hh_applicant_tool.storage import pgconn
from hh_applicant_tool.ai import ChatOpenAI
import httpx

FRESH_DAYS = 5
MAX_LLM = int(os.environ.get("CRAWL_MAX", "600"))
MIN_LEN = 60
VAC_RE = re.compile(r"(ваканс|ищ[еуа][мт]|требу[ею]|нужен|нужна|нужны|в команд|на проект|зарплат|з/?п|оклад|доход|gross|нетто|руб|usd|eur|опыт от|грейд|middle|senior|junior|стаж|разработчик|инженер|developer|программист|аналитик|тестировщик|девопс|devops|дизайнер|менеджер|architect|lead|удал[её]н|гибрид|офис|remote|релокац|hiring|position|обязанност|требован|услови|стек|занятост|резюме|откли|пиши|контакт)", re.I)
def _looks_like_vacancy(text):  # дешёвый предфильтр: гнать через LLM только похожее на вакансию
    return len(text) >= MIN_LEN and bool(VAC_RE.search(text))

CATS = ("general","python","go","java","backend","frontend","ds_ml","devops","mobile","qa","gamedev","product","remote")
SYS = (
  "Разбери пост из Telegram-канала с IT-вакансиями. Верни СТРОГО JSON одной строкой:\n"
  '{"is_vacancy":true|false,"category":"<один из: ' + ",".join(CATS) + '>",'
  '"title":"<должность кратко>","contact":"<@username или t.me/.. рекрутёра/HR для отклика в ЛС, иначе пусто>",'
  '"salary":"<если есть, иначе пусто>","remote":true|false,'
  '"region":"<город или страна вакансии: Москва/Россия/Грузия/Казахстан и т.п.; \'удалёнка\' если remote без города; \'релокация\' если переезд; пусто если не указан>"}\n'
  "is_vacancy=false если дайджест/реклама/резюме/опрос/не вакансия. Также is_vacancy=false если роль НЕ из IT/диджитал (оператор, курьер, продавец, бьюти, простые онлайн-задания, продажи не в IT). "
  "contact — ТОЛЬКО прямой контакт ЧЕЛОВЕКА (@user/t.me/user/ссылка на профиль); НЕ бот, НЕ канал, НЕ форма/hh/сайт."
)

def _openai():
    for acc in ("8510841974","144968591","lexa","egor","179169614"):
        oa = (pgconn.app_config(acc) or {}).get("openai")
        if oa and oa.get("token"): return oa
    return None

def _channels():
    cat = json.loads(pgconn.get_setting("tg.catalog", "{}", account="_global"))
    chans = sorted({u for v in cat.values() for u in v.get("channels", [])})
    n = int(os.environ.get("CRAWL_N", "0"))
    return chans[:n] if n else chans

def _strip(s):
    s = re.sub(r"<br\s*/?>", "\n", s); s = re.sub(r"<[^>]+>", "", s)
    return _html.unescape(s).strip()

def _parse(h):
    out = []
    for block in re.split(r'(?=<div class="tgme_widget_message_wrap)', h):
        dp = re.search(r'data-post="[^"/]+/(\d+)"', block)
        if not dp: continue
        pid = int(dp.group(1))
        tx = (re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*</div>', block, re.S)
              or re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S))
        if not tx: continue
        raw = tx.group(1)
        links = re.findall(r'href="((?:https?://t\.me|tg://)[^"]+)"', raw)
        text = _strip(raw)
        tm = re.search(r'datetime="([^"]+)"', block)
        dt = None
        if tm:
            try: dt = datetime.fromisoformat(tm.group(1))
            except Exception: dt = None
        out.append((pid, dt, text, links))
    return out

def _norm_contact(c, links):
    c = (c or "").strip()
    if re.match(r"@[A-Za-z]\w{3,}$", c): return c
    m = re.search(r"t\.me/([A-Za-z]\w{3,})", c)
    if m and not m.group(1).lower().endswith("bot"): return "@" + m.group(1)
    for l in links:
        m = re.search(r"t\.me/([A-Za-z]\w{3,})", l)
        if m and not m.group(1).lower().endswith("bot") and m.group(1).lower() != "s":
            return "@" + m.group(1)
    return ""

async def main():
    oa = _openai()
    if not oa: print("tg_crawler: нет openai — стоп"); return
    channels = _channels()
    seen = pgconn.seen_keys("tg_crawl")
    cutoff = datetime.now(timezone.utc) - timedelta(days=FRESH_DAYS)
    conn = pgconn.connect(); cur = conn.cursor()
    llm = stored = scanned = 0
    async with httpx.AsyncClient(headers={"user-agent": "Mozilla/5.0"}, follow_redirects=True) as cl:
        for ch in channels:
            if llm >= MAX_LLM: print(f"tg_crawler: лимит LLM {MAX_LLM} — стоп на @{ch}"); break
            try:
                r = await cl.get(f"https://t.me/s/{ch}", timeout=15)
                if r.status_code != 200: continue
            except Exception: continue
            for pid, dt, text, links in _parse(r.text):
                key = f"{ch}:{pid}"
                if key in seen: continue
                if (dt and dt < cutoff) or not _looks_like_vacancy(text):
                    pgconn.add_seen("tg_crawl", key); seen.add(key); continue
                if llm >= MAX_LLM: break
                scanned += 1
                try:
                    chat = ChatOpenAI(token=oa["token"], model=oa.get("model"),
                                      completion_endpoint=oa.get("completion_endpoint"),
                                      system_prompt=SYS, temperature=0, max_completion_tokens=300)
                    resp = (await chat.send_message(text[:2500]) or "").strip()
                    llm += 1
                    mm = re.search(r"\{.*\}", resp, re.S)
                    d = json.loads(mm.group(0)) if mm else {}
                except Exception: continue
                pgconn.add_seen("tg_crawl", key); seen.add(key)
                if not d.get("is_vacancy"): continue
                cat = d.get("category") if d.get("category") in CATS else "general"
                contact = _norm_contact(d.get("contact"), links)
                if contact.lower() == "@" + ch.lower(): contact = ""
                if not contact:
                    continue  # без контакта не храним — связаться нельзя
                cur.execute(
                    "INSERT INTO tg_vacancies (channel,post_id,posted_at,text,is_vacancy,category,title,contact,contact_type,salary,remote,region,post_url) "
                    "VALUES (%s,%s,%s,%s,true,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (channel,post_id) DO NOTHING",
                    (ch, pid, dt, text[:4000], cat, (d.get("title") or "")[:200], contact,
                     ("dm" if contact else "none"), (d.get("salary") or "")[:100], bool(d.get("remote")),
                     (d.get("region") or "")[:60], f"https://t.me/{ch}/{pid}"))
                stored += 1
                if stored % 25 == 0: conn.commit()
            await asyncio.sleep(0.3)
    conn.commit(); conn.close()
    try: pgconn.record_health("tg_crawl", True, f"scanned={scanned} llm={llm} stored={stored}", "_global")
    except Exception:
        try: pgconn.record_health("tg_crawl", True, f"scanned={scanned} llm={llm} stored={stored}")
        except Exception: pass
    print(f"tg_crawler: каналов {len(channels)}, оценено {scanned}, LLM {llm}, сохранено вакансий {stored}")

if __name__ == "__main__":
    asyncio.run(main())
