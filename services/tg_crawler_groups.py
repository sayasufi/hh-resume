"""
services/tg_crawler_groups.py — краулер групп-чатов (Фаза 2).
Аккаунт-краулер 8510841974 (@fffgergerg) ВСТУПАЕТ в группы-чаты каталога (постепенно,
JOIN_N/прогон — вступление флудит, аккаунт новый) и читает их Telethon'ом (joined → entity
в диалогах → без резолва → без флуда). Вакансии → tg_vacancies, контакт = АВТОР поста (ему в ЛС).
Дедуп seen_keys('tg_crawl') (общий с broadcast-краулером). Env: JOIN_N, CRAWL_MAX.
Запуск: python /app/services/tg_crawler_groups.py
"""
import sys, os, re, json, asyncio
from datetime import datetime, timezone, timedelta
sys.path.insert(0, "/app")
from hh_applicant_tool.storage import pgconn
from hh_applicant_tool.ai import ChatOpenAI
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.tl.types import User
from telethon.errors import FloodWaitError, UserAlreadyParticipantError

CRAWLER = "8510841974"
JOIN_PER_RUN = int(os.environ.get("JOIN_N", "5"))
JOIN_DELAY = int(os.environ.get("JOIN_DELAY", "25"))
FRESH_DAYS = 5
POSTS_PER_CH = 60
MAX_LLM = int(os.environ.get("CRAWL_MAX", "500"))
MIN_LEN = 60
VAC_RE = re.compile(r"(ваканс|ищ[еуа][мт]|требу[ею]|нужен|нужна|нужны|в команд|на проект|зарплат|з/?п|оклад|доход|gross|нетто|руб|usd|eur|опыт от|грейд|middle|senior|junior|стаж|разработчик|инженер|developer|программист|аналитик|тестировщик|девопс|devops|дизайнер|менеджер|architect|lead|удал[её]н|гибрид|офис|remote|релокац|hiring|position|обязанност|требован|услови|стек|занятост|резюме|откли|пиши|контакт)", re.I)
def _looks_like_vacancy(text):  # дешёвый предфильтр: гнать через LLM только похожее на вакансию
    return len(text) >= MIN_LEN and bool(VAC_RE.search(text))

GROUPS = ["python_jobs","java_jobs","golang_jobs","golang_jobsgo","php_jobs","scala_jobs","rust_jobs","qa_jobs","qajobsru","react_js_jobs","reactjs_jobs","react_native_jobs","javascript_jobs","nodejs_jobs","kotlinmppjobs","mobile_jobs","mobile_vacancies","gdtalents","gamedevjobtinder","cvjobge","uzjobit","georgiaitjobs","itkazahstan","jobgeeks","jobs_it","myjobit","microsoftstackjobs","mindset_jobs","products_jobs","projects_jobs","projects_jobs_feed","python_django_work","sysadm_in_job","sysadmin_rabota","tzprofi_job","relocaty_jobs","analysts_hunter","gogetajob","front_end_jobs","django_jobs","agile_jobs","android_jobs","datajobs","devops_jobs"]
CATS = ("general","python","go","java","backend","frontend","ds_ml","devops","mobile","qa","gamedev","product","remote","design")
SYS = (
  "Разбери пост из Telegram-чата с IT-вакансиями. Верни СТРОГО JSON одной строкой:\n"
  '{"is_vacancy":true|false,"category":"<один из: ' + ",".join(CATS) + '>",'
  '"title":"<должность кратко>","contact":"<@username/t.me рекрутёра, иначе пусто>",'
  '"salary":"<если есть>","remote":true|false,'
  '"region":"<город или страна вакансии: Москва/Россия/Грузия/Казахстан и т.п.; \'удалёнка\' если remote без города; \'релокация\' если переезд; пусто если не указан>"}\n'
  "is_vacancy=false если это болтовня/вопрос/резюме/реклама/не вакансия. Также is_vacancy=false если роль НЕ из IT/диджитал (оператор, курьер, продавец, бьюти, простые онлайн-задания, продажи не в IT)."
)

def _openai():
    for acc in (CRAWLER,"144968591","lexa","egor"):
        oa=(pgconn.app_config(acc) or {}).get("openai")
        if oa and oa.get("token"): return oa
    return None

def _catalog_groups():
    conn=pgconn.connect();cur=conn.cursor()
    cur.execute("SELECT username,category FROM tg_channels WHERE status='active' AND type='chat'")
    rows=cur.fetchall(); conn.close()
    cat_of={u.lower():(c or "general") for u,c in rows}
    return [u for u,c in rows], cat_of

async def _llm(oa,text):
    chat=ChatOpenAI(token=oa["token"],model=oa.get("model"),completion_endpoint=oa.get("completion_endpoint"),system_prompt=SYS,temperature=0,max_completion_tokens=300)
    resp=(await chat.send_message(text[:2500]) or "").strip()
    mm=re.search(r"\{.*\}",resp,re.S)
    return json.loads(mm.group(0)) if mm else {}

async def main():
    oa=_openai()
    if not oa: print("нет openai"); return
    groups,cat_of=_catalog_groups()
    cfg=pgconn.app_config(CRAWLER)
    client=TelegramClient(StringSession(pgconn.dec_session(cfg["tg_user_session"])), *pgconn.tg_api())
    await client.connect()
    if not await client.is_user_authorized(): print("сессия краулера не авторизована"); return
    joined={}
    async for d in client.iter_dialogs():
        u=getattr(d.entity,"username",None)
        if u: joined[u.lower()]=d.entity
    todo=[g for g in groups if g.lower() not in joined]
    print(f"групп каталога: {len(groups)}, уже вступлено: {sum(1 for g in groups if g.lower() in joined)}, осталось вступить: {len(todo)}")
    jn=0
    for g in todo[:JOIN_PER_RUN]:
        try:
            ent=await client.get_entity(g)
            await client(functions.channels.JoinChannelRequest(ent))
            joined[g.lower()]=ent; jn+=1; print(f"  вступил в @{g}")
        except UserAlreadyParticipantError:
            try: joined[g.lower()]=await client.get_entity(g)
            except Exception: pass
        except FloodWaitError as e:
            print(f"  !!! FLOOD {e.seconds}s на вступлении — стоп вступлений"); break
        except Exception as ex: print(f"  x @{g}: {type(ex).__name__}")
        await asyncio.sleep(JOIN_DELAY)
    seen=pgconn.seen_keys("tg_crawl")
    cutoff=datetime.now(timezone.utc)-timedelta(days=FRESH_DAYS)
    conn=pgconn.connect(); cur=conn.cursor()
    llm=stored=scanned=0
    for g in groups:
        ent=joined.get(g.lower())
        if not ent or llm>=MAX_LLM: continue
        try: msgs=await client.get_messages(ent,limit=POSTS_PER_CH)
        except Exception: continue
        for m in msgs:
            if llm>=MAX_LLM: break
            text=m.message or ""; key=f"{g}:{m.id}"
            if key in seen: continue
            if (m.date and m.date<cutoff) or not _looks_like_vacancy(text):
                pgconn.add_seen("tg_crawl",key); seen.add(key); continue
            scanned+=1
            try: d=await _llm(oa,text); llm+=1
            except Exception: continue
            pgconn.add_seen("tg_crawl",key); seen.add(key)
            if not d.get("is_vacancy"): continue
            au=m.sender.username if isinstance(m.sender,User) else None
            contact=("@"+au) if au else ""
            if not contact:
                c=(d.get("contact") or "").strip()
                mm=re.search(r"@[A-Za-z]\w{3,}",c) or re.search(r"t\.me/([A-Za-z]\w{3,})",c)
                if mm: contact = mm.group(0) if mm.group(0).startswith("@") else "@"+mm.group(1)
            if (contact or "").lower().lstrip("@") in ("gmail","yandex","mail","outlook","icloud","hotmail","ya","bk","list","inbox","rambler","proton","protonmail"): contact=""
            if not contact: continue  # без контакта не храним
            catg=d.get("category") if d.get("category") in CATS else cat_of.get(g.lower(),"general")
            cur.execute("INSERT INTO tg_vacancies (channel,post_id,posted_at,text,is_vacancy,category,title,contact,contact_type,salary,remote,region,post_url) "
              "VALUES (%s,%s,%s,%s,true,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (channel,post_id) DO NOTHING",
              (g,m.id,m.date,text[:4000],catg,(d.get("title") or "")[:200],contact,("author" if au else ("dm" if contact else "none")),
               (d.get("salary") or "")[:100],bool(d.get("remote")),(d.get("region") or "")[:60],f"https://t.me/{g}/{m.id}"))
            stored+=1
            if stored%25==0: conn.commit()
        await asyncio.sleep(0.5)
    conn.commit(); conn.close(); await client.disconnect()
    try: pgconn.record_health("tg_crawl_groups",True,f"joined+={jn} scanned={scanned} stored={stored}","_global")
    except Exception: pass
    print(f"группы: вступил +{jn}, в работе {sum(1 for g in groups if g.lower() in joined)}, оценено {scanned}, LLM {llm}, сохранено {stored}")

if __name__=="__main__": asyncio.run(main())
