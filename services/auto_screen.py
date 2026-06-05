"""Обобщённый авто-обработчик «дел» в Telegram через Telethon-сессию кандидата:
проходит боты-скринеры (LLM отвечает по резюме) и пишет HR. Переиспользует
giga_recruiter (промпт SYS_TMPL, LLM _answer, ожидание/кнопки/оценка).

БЕЗОПАСНОСТЬ: DRY по умолчанию — компонует ответы/сообщения и логгирует, НИЧЕГО не
отправляет (нужен явный --live). В dry боты: только /start, чтобы увидеть вопрос;
ответ НЕ шлётся, на согласии стоп. Giga-бот исключён (его проходит сам ГР).
"""
import asyncio
import re
import sys

import giga_recruiter as gr
from hh_applicant_tool.ai import ChatOpenAI
from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import StartBotRequest

LIVE = "--live" in sys.argv
DRY = not LIVE
TME = re.compile(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)(?:\?start=([\w=-]+))?")
ATRE = re.compile(r"@([A-Za-z0-9_]{4,})")
CONSENT_RE = re.compile(r"соглас|ознаком|принима|начать|продолж|поех|да[,!. ]", re.I)
MAX_TASKS = 6
MAX_TURNS = 22  # анкеты-скрининги бывают длинными (10-15+ вопросов)
REPLY_TIMEOUT = 70  # AI-боты (напр. «Василиса») генерят следующий вопрос медленно

HR_SYS = (
    "Ты — кандидат {name}. Напиши короткое, живое, ВЕЖЛИВОЕ первое сообщение HR в Telegram по "
    "вакансии «{vac}» — 2-3 коротких предложения. К HR обращайся ТОЛЬКО на «вы».\n"
    "ОБЯЗАТЕЛЬНО: поздоровайся формально — «Здравствуйте» или «Добрый день/вечер» (НИКОГДА не "
    "«Привет»); представься по ИМЕНИ (только имя, не ФИО целиком); скажи, что откликнулся на эту "
    "вакансию на hh; ОДНА простая фраза кто ты по специальности (строго по опыту ниже).\n"
    "НЕЛЬЗЯ: «Привет», тыканье HR («ты»), канцелярит и буззворды («большой опыт проектирования "
    "высоконагруженных систем», «экспертиза», «полного цикла»), markdown, выдуманные факты, слово "
    "«резюме» (ссылку добавят отдельно), заглушки [..].\n\n=== ОПЫТ ===\n{resume}\n=== КОНЕЦ ===")


def _hh_api(cfg):
    t = cfg["token"]
    return ApiClient(
        access_token=t["access_token"], refresh_token=t["refresh_token"],
        access_expires_at=t["access_expires_at"],
        user_agent=generate_android_useragent(), refresh_hook=pgconn.locked_token_refresh)


async def _last_employer_msg(api, nid):
    try:
        m = await api.get(f"/negotiations/{nid}/messages", page=0)
        p = m.get("pages", 1)
        if p > 1:
            m = await api.get(f"/negotiations/{nid}/messages", page=p - 1)
    except Exception:
        return ""
    emp = [x for x in (m.get("items") or [])
           if x.get("text") and x["author"]["participant_type"] == "employer"]
    return "\n".join(x["text"] for x in emp[-2:]) if emp else ""


def _pending(account):
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, action, action_url, vacancy, nid, vacancy_url FROM action_items "
                "WHERE account=%s AND coalesce(done,false)=false AND nid IS NOT NULL "
                "ORDER BY created_at DESC", (account,))
            return [{"id": r[0], "action": r[1] or "", "action_url": r[2] or "",
                     "vac": r[3] or "", "nid": r[4], "vac_url": r[5] or ""}
                    for r in cur.fetchall()]
    finally:
        conn.close()


def _mark_done(aid):
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE action_items SET done=true WHERE id=%s", (aid,))
        conn.commit()
    finally:
        conn.close()


async def _do_bot(client, oa, sys_prompt, bot, start, vac, dry):
    try:
        ent = await client.get_entity(bot)
    except Exception as e:
        print(f"    @{bot}: не резолвится ({type(e).__name__}) — пропуск")
        return False
    recent = await client.get_messages(ent, limit=4)
    inbound = sorted([m for m in recent if not m.out], key=lambda x: x.id)
    last = inbound[-1] if inbound else None
    # если скрининг уже идёт (последнее сообщение бота — незакрытый вопрос) — ПРОДОЛЖАЕМ
    # с него, а не перезапускаем /start (иначе бот переспросит всё заново).
    resume = bool(last and (last.buttons or "?" in (last.text or ""))
                  and not (gr.DONE_RE.search(last.text or "") and "?" not in (last.text or ""))
                  and not gr.NOACTIVE_RE.search(last.text or ""))
    if resume:
        print(f"    @{bot}: продолжаю незаконченный скрининг (не рестартю)")
        last_id = inbound[-2].id if len(inbound) >= 2 else 0
        seeded = [m for m in inbound if m.id > last_id]
    else:
        last_id = recent[0].id if recent else 0
        if start:
            await client(StartBotRequest(bot=ent, peer=ent, start_param=start))
        else:
            await client.send_message(ent, "/start")
        seeded = None
    convo, turns = [], 0
    while turns < MAX_TURNS:
        if seeded is not None:
            replies, seeded = seeded, None
        else:
            replies = await gr._wait_reply(client, ent, last_id, REPLY_TIMEOUT)
        if not replies:
            print(f"    @{bot}: бот молчит — стоп")
            return turns > 0
        for m in replies:
            last_id = max(last_id, m.id)
        if gr._star_msg(replies) is not None:
            if not dry:
                await gr._rate5(gr._star_msg(replies))
            print(f"    @{bot}: завершено (оценка 5★{' [dry]' if dry else ''})")
            return True
        bot_text = "\n".join((m.text or "").strip() for m in replies
                             if (m.text or "").strip()).strip()
        # кнопка согласия/старта — кликаем по ИНДЕКСУ (надёжнее, чем по тексту с эмодзи)
        consent_btn = None
        for m in replies:
            for i, row in enumerate(m.buttons or []):
                for j, b in enumerate(row):
                    if b.text and CONSENT_RE.search(b.text):
                        consent_btn = (m, i, j, b.text)
                        break
                if consent_btn:
                    break
            if consent_btn:
                break
        if consent_btn and "?" not in bot_text:
            mm, i, j, btext = consent_btn
            print(f"    @{bot}: кнопка [{btext}]{' [dry: не жму, стоп]' if dry else ' -> жму'}")
            if dry:
                return None
            try:
                await mm.click(i, j)
            except Exception as e:
                print(f"    @{bot}: клик не прошёл: {type(e).__name__}: {e}")
            await asyncio.sleep(3)
            continue
        if not bot_text:
            continue
        if gr.DONE_RE.search(bot_text) and "?" not in bot_text:
            print(f"    @{bot}: бот закончил")
            return True
        # вопрос с кнопками-вариантами (Да/Нет, выбор) -> LLM выбирает кнопку, НЕ текст
        opt_btns = [(m, i, j, b.text) for m in replies
                    for i, row in enumerate(m.buttons or [])
                    for j, b in enumerate(row) if b.text]
        if opt_btns:
            options = [t for (_, _, _, t) in opt_btns]
            pick = await _pick_button(oa, sys_prompt, bot_text, options)
            tgt = (next((x for x in opt_btns if x[3].strip() == pick.strip()), None)
                   or next((x for x in opt_btns if pick and (pick.lower() in x[3].lower()
                            or x[3].lower() in pick.lower())), None))
            if tgt:
                mm, i, j, bt = tgt
                print(f"    @{bot}\n      Q(кнопки {options}): «{bot_text[:90]}»\n      ВЫБОР: «{bt}»")
                if dry:
                    return None
                try:
                    await mm.click(i, j)
                except Exception as e:
                    print(f"    @{bot}: клик не прошёл: {type(e).__name__}")
                turns += 1
                await asyncio.sleep(3)
                continue
        convo.append("Рекрутёр: " + bot_text)
        answer = (await gr._answer(oa, sys_prompt, convo, bot_text) or "").strip()
        print(f"    @{bot}\n      Q: «{bot_text[:110]}»\n      A: «{answer[:200]}»")
        if not answer:
            return False
        if dry:
            print(f"    @{bot}: DRY — ответ НЕ отправлен")
            return None
        convo.append("Я: " + answer)
        s = await client.send_message(ent, answer, link_preview=False)
        last_id = max(last_id, s.id)
        turns += 1
        await asyncio.sleep(3)
    return True


async def _pick_button(oa, sys_prompt, question, options):
    """Вопрос с кнопками-вариантами (Да/Нет, выбор) — LLM выбирает кнопку по реальному опыту."""
    chat = ChatOpenAI(
        token=oa["token"], model=oa.get("model"),
        completion_endpoint=oa.get("completion_endpoint"),
        system_prompt=sys_prompt, temperature=0.1, max_completion_tokens=24)
    prompt = (f"Вопрос рекрутёра: «{question[:600]}»\nВарианты (кнопки): {options}\n"
              "Выбери ОДИН вариант, верный для тебя по твоему реальному опыту. "
              "Ответь ТОЛЬКО точным текстом одной кнопки, без кавычек и пояснений.")
    return ((await chat.send_message(prompt)) or "").strip()


async def _do_hr(client, oa, name, resume, hh_url, vac_url, user, vac, dry):
    chat = ChatOpenAI(
        token=oa["token"], model=oa.get("model"),
        completion_endpoint=oa.get("completion_endpoint"),
        system_prompt=HR_SYS.format(name=name, vac=vac, resume=resume),
        temperature=0.5, max_completion_tokens=180)
    intro = ((await chat.send_message(f"Вакансия: {vac}. Напиши первое сообщение HR.")) or "").strip()
    if not intro:
        return False
    # сразу даём ссылку на вакансию (откуда пришёл) и резюме — чтобы HR нашла отклик и поняла кто пишет
    msg = intro + (f"\nВакансия: {vac_url}" if vac_url else "") \
              + (f"\nМоё резюме: {hh_url}" if hh_url else "")
    print(f"    @{user} (вакансия «{vac[:40]}»)\n      СООБЩЕНИЕ: «{msg[:300]}»")
    if dry:
        print(f"    @{user}: DRY — сообщение НЕ отправлено")
        return None
    try:
        ent = await client.get_entity(user)
        await client.send_message(ent, msg, link_preview=False)
        return True
    except Exception as e:
        print(f"    @{user}: не отправилось ({type(e).__name__})")
        return False


async def main():
    cfg = pgconn.app_config()
    account = pgconn.get_account()
    enc = cfg.get("tg_user_session")
    oa = cfg.get("openai") or {}
    if not enc or not oa.get("token") or not (cfg.get("token") or {}).get("access_token"):
        print("auto_screen: нет tg-сессии / openai / hh-токена — пропуск")
        return
    name = gr._label()
    resume = (cfg.get("resume_text") or "").strip()
    if not resume:
        print("auto_screen: ВНИМАНИЕ — нет resume_text, ответы будут общими")
    sal = str((cfg.get("preferences") or {}).get("salary") or "").strip()
    salary_str = (f"{sal} рублей на руки" if sal.replace(" ", "").isdigit()
                  else (sal or "обсуждается, открыт к предложениям"))
    sys_prompt = gr.SYS_TMPL.format(name=name, salary=salary_str, resume=resume)
    # тон: к рекрутёру/боту всегда на «вы», вежливо; здороваться «Здравствуйте»/«Добрый день», не «Привет»
    sys_prompt += ("\n\nТОН: обращайся к рекрутёру/боту всегда на «вы», вежливо. Если здороваешься — "
                   "«Здравствуйте» или «Добрый день/вечер», НИКОГДА не «Привет».")

    tasks = _pending(account)
    print(f"auto_screen[{account}] режим={'LIVE' if LIVE else 'DRY (ничего не шлётся)'}: "
          f"pending дел {len(tasks)}")
    api = _hh_api(cfg)
    # реальная ссылка на hh-резюме — когда бот/HR просит «приложи резюме», дать её, не уклоняться
    hh_url = ""
    rid = pgconn.get_setting("apply.resume_id", account=account)
    if rid:
        try:
            _r = await api.get(f"/resumes/{rid}")
            hh_url = _r.get("alternate_url") or f"https://hh.ru/resume/{rid}"
        except Exception:
            hh_url = f"https://hh.ru/resume/{rid}"
        sys_prompt += (
            f"\n\nКогда просят ссылку на твоё резюме на hh.ru — дай ИМЕННО эту ссылку: {hh_url}. "
            "Не пиши «не могу прислать» и не уклоняйся. (Другие ссылки/GitHub/портфолио не выдумывай.)")
    api_id, api_hash = pgconn.tg_api()
    client = TelegramClient(StringSession(pgconn.dec_session(enc)), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("auto_screen: tg-сессия слетела — пропуск")
        await api.aclose()
        return
    nb = nh = 0
    seen = set()
    try:
        for t in tasks:
            if nb + nh >= MAX_TASKS:
                break
            msg = await _last_employer_msg(api, t["nid"])
            blob = f"{t['action']} {t['action_url']} {msg}"
            mt = TME.search(blob)
            if mt and mt.group(1).lower().endswith("bot") and "giga" not in mt.group(1).lower():
                bot = mt.group(1)
                if bot in seen:
                    continue
                seen.add(bot)
                print(f"\n  [БОТ] дело #{t['id']} «{t['vac'][:42]}» -> @{bot}")
                r = await _do_bot(client, oa, sys_prompt, bot, mt.group(2), t["vac"], DRY)
                if r is True and LIVE:
                    _mark_done(t["id"])
                nb += 1
            else:
                ma = ATRE.search(t["action"]) or ATRE.search(msg)
                low = t["action"].lower()
                if ma and ("напиш" in low or "telegram" in low or "@" in t["action"]):
                    user = ma.group(1)
                    print(f"\n  [HR] дело #{t['id']} «{t['vac'][:42]}» -> @{user}")
                    r = await _do_hr(client, oa, name, resume, hh_url, t["vac_url"], user, t["vac"], DRY)
                    if r is True and LIVE:
                        _mark_done(t["id"])
                    nh += 1
    finally:
        await client.disconnect()
        await api.aclose()
    print(f"\nauto_screen: ботов {nb}, HR {nh} "
          f"({'DRY — ничего не отправлено' if DRY else 'LIVE — отправлено'})")


if __name__ == "__main__":
    asyncio.run(main())
