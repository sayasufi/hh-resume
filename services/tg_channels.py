"""Источник: Telegram-каналы с вакансиями.

Читает настроенные каналы (userbot-сессия кандидата). По каждому новому посту LLM решает:
одиночная ли это вакансия под профиль кандидата (не дайджест/реклама) и есть ли в посте контакт
для отклика (@username рекрутёра). Если да — пишем рекрутёру персональное ЛС + ссылку на резюме.
Если в посте бот-ссылка для отклика — заводим «Дело» (его добьёт auto_screen, он source-agnostic).
Иначе — скип. Дедуп по `канал:post_id` (seen_keys 'tg_channels'), жёсткий rate-limit (холодные ЛС).

DRY по умолчанию (как auto_screen) — реально пишет только с --live.
Гейт: feat.tg_channels + tg_user_session. Запуск: python /app/services/tg_channels.py [--live]
"""
import asyncio
import random
import re
import sys
from datetime import datetime, timedelta, timezone

from hh_applicant_tool.ai import ChatOpenAI
from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeFilename

LIVE = "--live" in sys.argv
DRY = not LIVE
MAX_DM = 6              # холодных ЛС за прогон — spam-safety (Telegram флагает рассылку незнакомцам)
MAX_EVAL = 60          # потолок LLM-оценок постов за прогон (стоимость)
POSTS_PER_CH = 12      # сколько свежих постов смотреть на канал
FRESH_DAYS = 3         # посты старше — не трогаем
TME = re.compile(r"t\.me/([A-Za-z0-9_]{4,32})")

SYS = (
    "Тебе дают пост из Telegram-канала с IT-вакансиями. Оцени его относительно опыта кандидата ниже.\n"
    "Верни СТРОГО три строки:\n"
    "MATCH: да — если это ОДНА конкретная вакансия и кандидат подходит по ОСНОВНОМУ языку/направлению "
    "(например backend Python) и уровню. Узкий ДОМЕН вакансии (AI/ML, финтех, gamedev, e-com и т.п.) "
    "может НЕ совпадать с прошлым опытом кандидата — это ОК, домен вторичен. "
    "нет — если: дайджест из многих вакансий, реклама/курс/инфопродукт, не про конкретную работу; "
    "ИЛИ другой основной язык/стек (не его); ИЛИ другая профессия (для backend-разработчика НЕ подходят "
    "чисто Data Scientist/ML-research, аналитик, QA, дизайн, менеджмент); ИЛИ уровень явно не тянется "
    "(требуют существенно больше опыта, чем у кандидата).\n"
    "CONTACT: @username — если в посте есть прямой Telegram-контакт рекрутёра/нанимающего для отклика "
    "(«пишите @...», «резюме @...»); иначе НЕТ. НЕ бери @каналы/@ботов и не выдумывай.\n"
    "ПИСЬМО: <если MATCH=да и есть CONTACT — ОЧЕНЬ короткое деловое сообщение в ОДНУ строку: начни РОВНО с "
    "приветствия «{greet}», затем интерес к вакансии (назови её) и готовность обсудить/созвониться — и всё. "
    "НЕ описывай опыт, навыки, стек, проекты и не объясняй почему подходишь — резюме приложено отдельным файлом, "
    "оно само всё расскажет. Без markdown, без слова «резюме». Пример: «{greet}! Заинтересовала ваша вакансия X, "
    "буду рад обсудить детали». Иначе: ->>\n\n"
    "=== ОПЫТ КАНДИДАТА ===\n{resume}\n=== КОНЕЦ ===")


def _strip(s):
    return re.sub(r"\s+", " ", s or "").strip()


async def _decide(oa, resume, post, greet="Здравствуйте"):
    """LLM -> (match: bool, contact: '@x'|'', letter: str). greet — приветствие по времени отправки."""
    if not (oa and oa.get("token") and resume):
        return False, "", ""
    try:
        chat = ChatOpenAI(token=oa["token"], model=oa.get("model"),
                          completion_endpoint=oa.get("completion_endpoint"),
                          system_prompt=SYS.format(resume=resume[:3000], greet=greet),
                          temperature=0.1, max_completion_tokens=320)
        t = ((await chat.send_message(post[:2500])) or "").strip()
    except Exception as e:
        print(f"  LLM err {type(e).__name__}")
        return False, "", ""
    match, contact, letter = False, "", ""
    for line in t.splitlines():
        s = line.strip()
        up = s.upper()
        if up.startswith("MATCH:"):
            match = "да" in s.lower()
        elif up.startswith("CONTACT:"):
            v = s.split(":", 1)[1].strip()
            m = re.search(r"@([A-Za-z0-9_]{4,32})", v)
            if m and not m.group(1).lower().endswith("bot"):
                contact = m.group(1)
        elif up.startswith("ПИСЬМО:"):
            v = s.split(":", 1)[1].strip()
            if v and len(v) >= 15 and "->" not in v:
                letter = v
    return match, contact, letter


def _channels(account):
    """Каналы = объединение выбранных категорий каталога + (опц.) кастомные каналы."""
    import json
    cats_raw = pgconn.get_setting("tg.cats", account=account)
    if cats_raw is None:  # не настраивал -> дефолтные категории
        cats_raw = pgconn.get_setting("tg.cats_default", account="_global") or ""
    cats = [c.strip() for c in cats_raw.split(",") if c.strip()]
    catalog = json.loads(pgconn.get_setting("tg.catalog", account="_global") or "{}")
    out, seen = [], set()
    for k in cats:
        for u in (catalog.get(k, {}).get("channels") or []):
            if u not in seen:
                seen.add(u)
                out.append(u)
    for c in (pgconn.get_setting("tg.channels", account=account) or "").split(","):  # кастомные
        u = c.strip().lstrip("@")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def _hh_resume_url(cfg, account):
    rid = pgconn.get_setting("apply.resume_id", account=account)
    tok = (cfg.get("token") or {})
    if not (rid and tok.get("access_token")):
        return ""
    api = ApiClient(access_token=tok["access_token"], refresh_token=tok.get("refresh_token"),
                    access_expires_at=tok.get("access_expires_at"),
                    user_agent=generate_android_useragent(), refresh_hook=pgconn.locked_token_refresh)
    try:
        r = await api.get(f"/resumes/{rid}")
        return r.get("alternate_url") or f"https://hh.ru/resume/{rid}"
    except Exception:
        return f"https://hh.ru/resume/{rid}"
    finally:
        await api.aclose()


async def _hh_resume_pdf(cfg, account):
    """Скачать PDF резюме с hh -> (путь, имя_файла=«Фамилия Имя Отчество.pdf») или (None, None)."""
    rid = pgconn.get_setting("apply.resume_id", account=account)
    tok = (cfg.get("token") or {})
    if not (rid and tok.get("access_token")):
        return None, None
    api = ApiClient(access_token=tok["access_token"], refresh_token=tok.get("refresh_token"),
                    access_expires_at=tok.get("access_expires_at"),
                    user_agent=generate_android_useragent(), refresh_hook=pgconn.locked_token_refresh)
    try:
        r = await api.get(f"/resumes/{rid}")
        url = (((r.get("download") or {}).get("pdf") or {}).get("url"))
        if not url:
            return None, None
        fio = " ".join(x for x in (r.get("last_name"), r.get("first_name"), r.get("middle_name")) if x).strip()
        fname = (fio + ".pdf") if fio else "resume.pdf"
        import httpx
        async with httpx.AsyncClient(timeout=40, follow_redirects=True) as h:
            resp = await h.get(url, headers={"Authorization": f"Bearer {tok['access_token']}",
                                             "User-Agent": generate_android_useragent()})
            resp.raise_for_status()
            data = resp.content
        if not data or len(data) < 1000:  # подозрительно мелкий -> не PDF
            return None, None
        path = f"/tmp/resume_{account}.pdf"
        with open(path, "wb") as f:
            f.write(data)
        print(f"  [PDF] резюме скачано ({len(data)//1024} КБ) -> {fname}")
        return path, fname
    except Exception as e:
        print(f"  [PDF] не скачалось: {type(e).__name__}")
        return None, None
    finally:
        await api.aclose()


def _outreach_contacts(account) -> set:
    """Кому уже писали (из tg_outreach) — для дедупа по рекрутёру."""
    conn = pgconn.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT lower(contact) FROM tg_outreach WHERE account=%s AND status='sent' AND contact<>''", (account,))
        return set(r[0] for r in cur.fetchall())
    except Exception:
        return set()
    finally:
        conn.close()


CATS_ALL = ("general", "python", "go", "java", "backend", "frontend", "ds_ml",
            "devops", "mobile", "qa", "gamedev", "product", "remote", "design")
CAT_SYS = (
    "Тебе дают резюме IT-кандидата. Определи, в каких нишах ему искать вакансии — куда он реально "
    "может откликаться по своему опыту. Ниши (ключи): general — общие IT; python; go; "
    "java — java/kotlin/scala; backend — php/c#/rust/ruby/1c/общий бэкенд; frontend — js/react/vue/angular; "
    "ds_ml — data/ml/ai/аналитика; devops — sre/сисадмин/инфра; mobile — ios/android/flutter; "
    "qa — тестирование; gamedev; product — продукт/проджект; design — ui/ux/графический/продуктовый дизайн; remote — удалёнка/релокация. "
    "Верни ТОЛЬКО ключи через запятую (2-5 самых релевантных). Всегда добавляй general. "
    "Если по опыту кандидат открыт к удалёнке — добавь remote."
)


async def _derive_cats(oa, resume):
    """LLM по резюме -> список ниш кандидата (ключи из CATS_ALL)."""
    if not (oa and oa.get("token") and resume):
        return []
    try:
        chat = ChatOpenAI(token=oa["token"], model=oa.get("model"),
                          completion_endpoint=oa.get("completion_endpoint"),
                          system_prompt=CAT_SYS, temperature=0, max_completion_tokens=60)
        t = ((await chat.send_message(resume[:3000])) or "").strip().lower()
    except Exception as e:
        print(f"tg_channels: авто-категории не вышли ({type(e).__name__})")
        return []
    out = []
    for c in re.split(r"[^a-z_]+", t):
        if c in CATS_ALL and c not in out:
            out.append(c)
    if out and "general" not in out:
        out.insert(0, "general")
    return out


def _cats(account):
    raw = (pgconn.get_setting("tg.cats", account=account)
           or pgconn.get_setting("tg.cats_default", account="_global") or "")
    return [c.strip() for c in raw.split(",") if c.strip()]


def _db_vacancies(cats, limit=150):
    """Свежие вакансии из tg_vacancies под категории кандидата, с прямым @контактом рекрутёра."""
    if not cats:
        return []
    conn = pgconn.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, channel, category, title, text, contact FROM tg_vacancies "
            "WHERE is_vacancy AND contact LIKE '@%%' AND category = ANY(%s) "
            "AND posted_at > now() - make_interval(days => 4) "
            "ORDER BY posted_at DESC LIMIT %s",
            (cats, limit))
        return cur.fetchall()
    finally:
        conn.close()


def _record_outreach(account, vid, channel, contact, title, category, letter, status):
    """Запись TG-отклика (кому/что написали). dry в DRY, sent в LIVE. Идемпотентно по (account, vac_id)."""
    conn = pgconn.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tg_outreach(account, vac_id, channel, contact, title, category, letter, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(account, vac_id) DO UPDATE SET status=excluded.status, "
            "contact=excluded.contact, title=excluded.title, letter=excluded.letter",
            (account, vid, channel, contact, (title or "")[:200], category, (letter or "")[:600], status))
        conn.commit()
    except Exception as e:
        print(f"  [tg_outreach] не записалось: {type(e).__name__}")
    finally:
        conn.close()


async def _check_replies(client, account):
    """Отметить в tg_outreach рекрутёров, которые ОТВЕТИЛИ (входящее сообщение в ЛС).
    iter_dialogs — без резолва, без флуда. Флаг только ставим (не снимаем)."""
    conn = pgconn.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT lower(contact) FROM tg_outreach WHERE account=%s AND status='sent' "
                    "AND NOT replied AND contact LIKE '@%%'", (account,))
        pending = {r[0] for r in cur.fetchall()}  # '@username' в нижнем регистре
    except Exception:
        conn.close()
        return
    if not pending:
        conn.close()
        return
    replied = set()
    try:
        async for d in client.iter_dialogs():
            u = getattr(d.entity, "username", None)
            if not u:
                continue
            key = "@" + u.lower()
            if key not in pending:
                continue
            msg = d.message
            if (msg is not None and not msg.out) or (d.unread_count or 0) > 0:
                replied.add(key)  # есть входящее / непрочитанное -> ответили
    except Exception:
        pass
    if replied:
        try:
            cur = conn.cursor()
            for key in replied:
                cur.execute("UPDATE tg_outreach SET replied=true, replied_at=now() "
                            "WHERE account=%s AND lower(contact)=%s", (account, key))
            conn.commit()
            print(f"  [ответы] отметил ответивших рекрутёров: {len(replied)}")
        except Exception:
            pass
    conn.close()


FOLDER_TITLE = "Отклики"


async def _ensure_outreach_folder(client, new_entities):
    """Сложить чаты с рекрутёрами (кому написали) в отдельную папку Telegram кандидата
    (создать если нет, доливать к существующим)."""
    from telethon.utils import get_input_peer
    from telethon.tl.functions.messages import GetDialogFiltersRequest, UpdateDialogFilterRequest
    from telethon.tl.types import DialogFilter
    new_peers = []
    for ent in new_entities:
        try:
            new_peers.append(get_input_peer(ent))
        except Exception:
            pass
    if not new_peers:
        return
    res = await client(GetDialogFiltersRequest())
    filters = getattr(res, "filters", res)

    def _tstr(f):
        t = getattr(f, "title", None)
        if t is None:
            return ""
        return getattr(t, "text", t) if not isinstance(t, str) else t

    def _pkey(p):
        return (getattr(p, "user_id", None), getattr(p, "channel_id", None), getattr(p, "chat_id", None))

    existing, ids = None, set()
    for f in filters:
        fid = getattr(f, "id", None)
        if isinstance(fid, int):
            ids.add(fid)
        if isinstance(f, DialogFilter) and _tstr(f) == FOLDER_TITLE:
            existing = f
    merged = list(getattr(existing, "include_peers", []) or []) if existing else []
    seen = {_pkey(p) for p in merged}
    for p in new_peers:
        if _pkey(p) not in seen:
            merged.append(p)
            seen.add(_pkey(p))
    merged = merged[:100]
    fid = existing.id if existing else (max([i for i in ids if i >= 2] + [1]) + 1)
    try:
        from telethon.tl.types import TextWithEntities
        title = TextWithEntities(text=FOLDER_TITLE, entities=[])
    except Exception:
        title = FOLDER_TITLE
    flt = DialogFilter(id=fid, title=title, pinned_peers=[], include_peers=merged, exclude_peers=[])
    await client(UpdateDialogFilterRequest(id=fid, filter=flt))
    print(f"  [папка] «{FOLDER_TITLE}» {'обновлена' if existing else 'создана'}: {len(merged)} чатов")


async def run():
    account = pgconn.get_account()
    if not pgconn.feature_enabled("tg_channels"):
        print("tg_channels: feat выключен — пропуск")
        return
    cfg = pgconn.app_config()
    enc = cfg.get("tg_user_session")
    oa = cfg.get("openai") or {}
    if not enc or not oa.get("token"):
        print("tg_channels: нет tg-сессии / openai — пропуск")
        return
    resume = (cfg.get("resume_text") or "").strip()
    if not resume:
        print("tg_channels: нет resume_text — пропуск (матчинг будет мусорным)")
        return
    # категории кандидата: явные (кабинет/ранее авто) ИЛИ авто-вывод из резюме (B)
    explicit = pgconn.get_setting("tg.cats", account=account)
    if explicit:
        cats = [c.strip() for c in explicit.split(",") if c.strip()]
    else:
        cats = await _derive_cats(oa, resume)
        if cats:
            pgconn.set_setting("tg.cats", ",".join(cats), account)
            print(f"tg_channels[{account}]: авто-категории из резюме -> {cats}")
        else:
            cats = _cats(account)  # фоллбэк на дефолт
    out_seen = pgconn.seen_keys(f"tg_out_{account}")
    vacs = [v for v in _db_vacancies(cats) if str(v[0]) not in out_seen]
    if not vacs:
        print(f"tg_channels[{account}]: нет свежих вакансий под {cats} — пропуск")
        return

    hh_url = await _hh_resume_url(cfg, account)
    pdf_path, pdf_name = await _hh_resume_pdf(cfg, account)   # PDF резюме + имя файла (ФИО) для LIVE
    done_contacts = _outreach_contacts(account)     # кому уже писали -> дедуп по рекрутёру (антиспам)
    # ЛС рекрутёру шлём ТОЛЬКО в LIVE. В DRY сессию кандидата вообще не подключаем —
    # гарантия, что эйчарам ничего не пишется, пока не разрешат.
    client = None
    if LIVE:
        api_id, api_hash = pgconn.tg_api()
        client = TelegramClient(StringSession(pgconn.dec_session(enc)), api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            print("tg_channels: tg-сессия слетела — пропуск")
            return
        await _check_replies(client, account)  # отметить, кто из рекрутёров ответил
    dm = evals = 0
    sent_entities = []  # чаты с рекрутёрами, кому написали -> в папку «Отклики» (LIVE)
    _h = (datetime.now(timezone.utc) + timedelta(hours=3)).hour  # МСК
    greet = ("Доброе утро" if 5 <= _h < 12 else "Добрый день" if 12 <= _h < 18
             else "Добрый вечер" if 18 <= _h < 23 else "Здравствуйте")
    print(f"tg_channels[{account}] режим={'LIVE' if LIVE else 'DRY'}: вакансий-кандидатов {len(vacs)} "
          f"(категории {cats}), резюме-PDF={'есть' if pdf_path else 'нет'}, приветствие={greet}")
    try:
        for vid, channel, category, title, text, contact in vacs:
            if dm >= MAX_DM or evals >= MAX_EVAL:
                break
            evals += 1
            match, c2, letter = await _decide(oa, resume, text, greet)
            if not match:
                pgconn.add_seen(f"tg_out_{account}", str(vid)); out_seen.add(str(vid))
                continue
            letter = _strip(letter)
            if not letter:  # без персонального текста НЕ пишем (дженерик-заглушку не шлём)
                pgconn.add_seen(f"tg_out_{account}", str(vid)); out_seen.add(str(vid))
                continue
            to = contact if (contact or "").startswith("@") else ("@" + c2 if c2 else "")
            if not to:
                continue
            if to.lower() in done_contacts:  # этому рекрутёру уже писали -> не дублируем
                pgconn.add_seen(f"tg_out_{account}", str(vid)); out_seen.add(str(vid))
                continue
            done_contacts.add(to.lower())
            if not LIVE:
                # DRY — НИЧЕГО не отправляем, только показываем что отправили бы (вакансию НЕ помечаем seen — уйдёт при LIVE)
                print(f"  [DRY ЛС→{to}{' +PDF' if pdf_path else ''}] {category}/{title[:42]} (из @{channel}): {letter[:90]}")
                _record_outreach(account, vid, channel, to, title, category, letter, "dry")
                dm += 1
                continue
            try:
                ent = await client.get_entity(to)
                if pdf_path:  # прикрепляем PDF-резюме (имя файла = ФИО), письмо — подписью
                    await client.send_file(ent, pdf_path, caption=letter, force_document=True,
                                           attributes=[DocumentAttributeFilename(pdf_name)])
                else:
                    await client.send_message(ent, letter + (f"\nМоё резюме: {hh_url}" if hh_url else ""), link_preview=False)
                pgconn.bump_activity("tg_channels", 1, account=account)
                pgconn.add_seen(f"tg_out_{account}", str(vid)); out_seen.add(str(vid))
                _record_outreach(account, vid, channel, to, title, category, letter, "sent")
                sent_entities.append(ent)  # для папки «Отклики»
                dm += 1
                print(f"  [ЛС{'+PDF' if pdf_path else ''}] написал {to} (из @{channel})")
            except FloodWaitError as e:
                # сессия во флуд-вейте — продолжать опасно (усугубим/бан). Фиксируем для
                # health-check (tg_session) и СТОП рассылки до следующего прогона.
                import time as _t
                pgconn.set_setting("tg_flood_until", str(int(_t.time()) + int(e.seconds)), account)
                pgconn.record_health("tg_session", False, f"флуд-вейт ~{int(e.seconds)//60} мин (рассылка)", account=account)
                print(f"  [ЛС] ФЛУД {e.seconds}s — стоп рассылки")
                break
            except Exception as e:
                print(f"  [ЛС] {to}: не отправилось ({type(e).__name__})")
            await asyncio.sleep(random.uniform(6, 16))
        if LIVE and client and sent_entities:  # чаты с рекрутёрами -> отдельная папка кандидата
            try:
                await _ensure_outreach_folder(client, sent_entities)
            except Exception as e:
                print(f"  [папка] {type(e).__name__}: {e}")
    finally:
        if client:
            await client.disconnect()
    pgconn.record_health("tg_channels", True,
                         f"{'LIVE' if LIVE else 'DRY'}: откликов {dm}, оценено {evals}", account=account)
    print(f"tg_channels: готово — {'ЛС' if LIVE else 'DRY-совпадений'} {dm}, оценено {evals}")


if __name__ == "__main__":
    asyncio.run(run())
