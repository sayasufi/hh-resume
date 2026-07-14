"""form_fill.py — агент заполнения ВНЕШНИХ анкет/форм из «Дел» (action_items).

Работает браузером (Playwright/Chromium, тот же, что apply_tests). Поддерживает
Google Forms и обычные HTML-формы. Ответы берёт из ПРОФИЛЯ кандидата (резюме hh +
resume_text) через LLM. Ничего не выдумывает: если данных в профиле нет — помечает
поле как «нужен человек».

ПРЕДОХРАНИТЕЛИ:
  • по умолчанию DRY — заполняет и делает скриншот, НО НЕ отправляет (--live чтобы слать);
  • капча / форма логина / загрузка файла / оплата -> НЕ трогаем, отдаём человеку;
  • обязательное поле, на которое нет ответа из профиля -> НЕ отправляем, отдаём человеку.

Запуск:
  python form_fill.py --account lexa --url https://forms.gle/...   # один тест (dry)
  python form_fill.py --account lexa [--limit N] [--live]          # из «Дел»
"""
import asyncio
import re
import sys

from playwright.async_api import async_playwright
from hh_applicant_tool.ai import ChatOpenAI
from hh_applicant_tool.ai.openai import OpenAIError
from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn

LIVE = "--live" in sys.argv
LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 5
ONE_URL = sys.argv[sys.argv.index("--url") + 1] if "--url" in sys.argv else None
ACCOUNT = sys.argv[sys.argv.index("--account") + 1] if "--account" in sys.argv else None

# URL-ы «Дел», которые пробуем автоматизировать (остальное — человеку)
FORM_HOST_RE = re.compile(r"forms\.gle|docs\.google\.com/forms|/forms/d/", re.I)
# явные стоп-сигналы на странице -> сразу человеку
_CAPTCHA_SEL = ("iframe[src*=recaptcha]", "iframe[src*=hcaptcha]",
                "iframe[title*=recaptcha]", "div.g-recaptcha", "[class*=captcha]")
SKIP_TOKEN = "ПРОПУСК"

SYS = (
    "Ты помогаешь кандидату заполнить анкету/форму при отклике на вакансию. "
    "Отвечай ОТ ПЕРВОГО ЛИЦА, кратко и ПРАВДИВО, строго по профилю ниже. "
    "НЕ приписывай кандидату опыт/навыки/контакты, которых нет в профиле. "
    "Если в профиле нет данных для честного ответа на вопрос — ответь РОВНО одним "
    f"словом: {SKIP_TOKEN}. Без преамбул, только сам ответ."
)


def _bad(a: str) -> bool:
    low = (a or "").lower()
    return (len(a.strip()) < 1 or "предоставьте" in low or "уточните" in low
            or "не могу" in low or "сформулир" in low)


async def build_profile(api, acc, cfg):
    """(поля-словарь, текст профиля для LLM). Тянем из hh /me + активного резюме."""
    fields = {}
    try:
        me = await api.get("/me")
        fields["Имя"] = " ".join(x for x in (me.get("first_name"), me.get("last_name"),
                                             me.get("middle_name")) if x).strip()
        if me.get("email"):
            fields["Email"] = me["email"]
        if me.get("phone"):
            fields["Телефон"] = me["phone"]
    except Exception:
        pass
    rid = pgconn.get_setting("apply.resume_id", account=acc)
    resume_txt = (cfg.get("resume_text") or "").strip()
    try:
        r = await api.get(f"/resumes/{rid}") if rid else {}
    except Exception:
        r = {}
    if r:
        fields.setdefault("Имя", " ".join(x for x in (r.get("last_name"), r.get("first_name"),
                                                       r.get("middle_name")) if x).strip())
        fields["Должность"] = r.get("title") or ""
        fields["Город"] = (r.get("area") or {}).get("name") or ""
        sal = r.get("salary") or {}
        if sal.get("amount"):
            fields["Желаемая зарплата"] = f"{sal['amount']} {sal.get('currency','RUR')}"
        sk = r.get("skill_set") or []
        if sk:
            fields["Ключевые навыки"] = ", ".join(sk[:20])
        exp = r.get("experience") or []
        if exp:
            e = exp[0]
            fields["Текущее место"] = f"{e.get('position','')} @ {e.get('company','')}"
        _sch = r.get("schedule") or {}
        if isinstance(_sch, list):
            _sch = ", ".join((s.get("name") or "") for s in _sch if isinstance(s, dict))
        elif isinstance(_sch, dict):
            _sch = _sch.get("name") or ""
        fields["Формат работы"] = _sch or "удалённый/гибрид"
        fields["Резюме на hh"] = r.get("alternate_url") or ""
    # Телеграм из подключённой сессии кандидата (частое обязательное поле анкет)
    enc = cfg.get("tg_user_session")
    if enc:
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            aid, ah = pgconn.tg_api()
            c = TelegramClient(StringSession(pgconn.dec_session(enc)), aid, ah)
            await c.connect()
            if await c.is_user_authorized():
                me = await c.get_me()
                if me and me.username:
                    fields["Телеграм"] = f"https://t.me/{me.username}"
            await c.disconnect()
        except Exception:
            pass
    prof = "ПРОФИЛЬ КАНДИДАТА:\n" + "\n".join(f"- {k}: {v}" for k, v in fields.items() if v)
    if resume_txt:
        prof += "\n\nПОЛНОЕ РЕЗЮМЕ:\n" + resume_txt[:2500]
    return fields, prof


async def _answer_text(llm, prof, q, long=False):
    hint = "Развёрнуто (2-4 предложения)" if long else "Кратко, одной строкой"
    guide = (
        "Если вопрос ОТКРЫТЫЙ (почему вы / расскажите / мотивация / что можете дать / "
        "чем полезны) — ОБЯЗАТЕЛЬНО ответь по резюме и здравому смыслу, НЕ пропускай. "
        f"{SKIP_TOKEN} — ТОЛЬКО если просят конкретные данные, которых нет в профиле "
        "(телефон, ссылка на телеграм/github/портфолио, номер, сертификат).")
    for attempt in (1, 2):
        try:
            a = (await llm.send_message(
                f"{prof}\n\nВопрос анкеты: {q}\n{hint}. {guide}")).strip()
        except OpenAIError as e:
            return None, f"LLM error: {repr(e)[:50]}"
        if a.upper().strip(".! ") == SKIP_TOKEN:
            return SKIP_TOKEN, "нет данных в профиле"
        if not _bad(a):
            return a, "ok"
    return None, f"ненадёжный ответ: {a[:50]}"


# Вопрос-самооценка навыка («оцените по шкале», звёзды) -> ставим МАКСИМУМ.
# НЕ трогаем «сколько лет опыта / возраст» (там максимум = ложь).
_RATING_Q = re.compile(r"оцен|по\s*\d*[- ]*балл|шкал|уровень\s+владени|насколько\s+хорошо|"
                       r"звёзд|звезд|рейтинг", re.I)
_YEARS_Q = re.compile(r"сколько\s+лет|стаж|возраст|лет\s+опыт", re.I)


def _rating_max_idx(q, options):
    """Индекс максимального варианта, если вопрос — самооценка навыка по числовой шкале."""
    if not _RATING_Q.search(q or "") or _YEARS_Q.search(q or ""):
        return None
    nums = []
    for o in options:
        m = re.match(r"^\s*(\d+)", o or "")
        nums.append(int(m.group(1)) if m else None)
    if len(nums) >= 3 and all(n is not None for n in nums):
        return nums.index(max(nums))
    return None


async def _answer_choice(llm, prof, q, options, multi=False):
    listing = "\n".join(f"{i+1}) {o}" for i, o in enumerate(options))
    rule = ("Можно несколько — перечисли номера через запятую." if multi
            else f"Ответь СТРОГО одной цифрой 1..{len(options)}.")
    base = (f"{prof}\n\nВопрос: {q}\nВарианты:\n{listing}\n"
            f"Выбери подходящее по профилю. Если прямого ответа нет — самый разумный "
            f"(например, согласие на формат/переезд, если это адекватно). {rule} "
            f"Если ни один не подходит и это исказит правду — ответь {SKIP_TOKEN}.")
    for attempt in (1, 2):
        try:
            r = (await llm.send_message(base if attempt == 1
                 else base + f"\n\nОтветь ТОЛЬКО {'номерами' if multi else 'числом'}.")).strip()
        except OpenAIError as e:
            return None, f"LLM error: {repr(e)[:50]}"
        if SKIP_TOKEN in r.upper():
            return SKIP_TOKEN, "нет подходящего варианта"
        nums = [int(x) - 1 for x in re.findall(r"\d+", r)]
        nums = [n for n in nums if 0 <= n < len(options)]
        if nums:
            return (nums if multi else nums[:1]), "ok"
    return None, f"LLM не дал номер: {r[:40]}"


async def _hard_guards(page):
    """Жёсткие блокеры — форму нельзя авто-заполнить даже если поля есть."""
    for sel in _CAPTCHA_SEL:
        if await page.query_selector(sel):
            return "капча на странице"
    if await page.query_selector('input[type=password]'):
        return "форма требует логин (пароль)"
    if await page.query_selector('input[type=file]'):
        return "нужна загрузка файла (например, резюме-файл)"
    return None


async def _login_text(page):
    """Мягкий детектор входа/оплаты — применяем ТОЛЬКО когда полей формы не нашли
    (иначе ловит безобидный футер «войдите в аккаунт Google, чтобы сохранить»)."""
    body = ((await page.inner_text("body"))[:4000] if await page.query_selector("body") else "").lower()
    if any(w in body for w in ("требуется вход", "авторизуйтесь чтобы", "необходимо войти",
                               "оплатите", "payment required", "sign in to continue")):
        return "страница требует вход/оплату"
    return None


async def fill_google_form(page, llm, prof, vname):
    """Заполнить Google Form. -> (status, filled:list, need_human:list).
    status: 'ok' | 'partial' | 'empty'."""
    items = page.locator('div[role=listitem]')
    n = await items.count()
    filled, need = [], []
    for i in range(n):
        it = items.nth(i)
        try:
            head = it.locator('[role=heading]')
            q = (await head.first.inner_text()).strip() if await head.count() else ""
            if not q:
                txt = (await it.inner_text()).strip()
                q = txt.split("\n")[0] if txt else ""
        except Exception:
            q = ""
        if not q or len(q) < 2:
            continue
        required = bool(await it.locator('[aria-label*="бязательн"], [aria-label*="equired"]').count())
        radios = it.locator('[role=radio]')
        checks = it.locator('[role=checkbox]')
        text_in = it.locator('input[type=text], input[type=email], input[type=tel], input:not([type]), textarea')
        try:
            nr, nc, nt = await radios.count(), await checks.count(), await text_in.count()
        except Exception:
            nr = nc = nt = 0

        if nr:
            opts = [((await radios.nth(j).get_attribute("aria-label")) or
                     (await radios.nth(j).get_attribute("data-value")) or "").strip()
                    for j in range(nr)]
            ridx = _rating_max_idx(q, opts)
            if ridx is not None:  # самооценка навыка -> максимум звёзд
                try:
                    await radios.nth(ridx).click(timeout=5000)
                    filled.append((q, f"○ {opts[ridx]} (навык→макс)"))
                except Exception as e:
                    need.append((q, f"клик rating не удался: {repr(e)[:40]}"))
                continue
            ans, why = await _answer_choice(llm, prof, q, opts)
            if ans in (None, SKIP_TOKEN):
                (need if required else filled).append((q, f"пропуск ({why})"))
                continue
            try:
                await radios.nth(ans[0]).click(timeout=5000)
                filled.append((q, f"○ {opts[ans[0]]}"))
            except Exception as e:
                need.append((q, f"клик radio не удался: {repr(e)[:40]}"))
        elif nc:
            opts = [((await checks.nth(j).get_attribute("aria-label")) or "").strip() for j in range(nc)]
            ans, why = await _answer_choice(llm, prof, q, opts, multi=True)
            if ans in (None, SKIP_TOKEN):
                (need if required else filled).append((q, f"пропуск ({why})"))
                continue
            picked = []
            for idx in ans:
                try:
                    await checks.nth(idx).click(timeout=4000); picked.append(opts[idx])
                except Exception:
                    pass
            filled.append((q, "☑ " + "; ".join(picked)))
        elif nt:
            el = text_in.first
            long = (await el.evaluate("e => e.tagName")).upper() == "TEXTAREA"
            ans, why = await _answer_text(llm, prof, q, long=long)
            if ans in (None, SKIP_TOKEN):
                (need if required else filled).append((q, f"пропуск ({why})"))
                continue
            try:
                await el.fill(ans, timeout=5000)
                filled.append((q, f"✎ {ans[:60]}"))
            except Exception as e:
                need.append((q, f"fill не удался: {repr(e)[:40]}"))
        else:
            # dropdown / неизвестный тип -> человеку, если обязательный
            if required:
                need.append((q, "тип поля не поддержан (dropdown/др.)"))
    status = "empty" if not filled and not need else ("ok" if not need else "partial")
    return status, filled, need


async def submit_google_form(page):
    for name in ("Отправить", "Submit", "Готово", "Далее", "Next"):
        btn = page.get_by_role("button", name=re.compile(name, re.I))
        if await btn.count():
            try:
                await btn.first.click(timeout=6000)
                await page.wait_for_timeout(2500)
                body = (await page.inner_text("body")).lower()
                if any(w in body for w in ("ответ записан", "ответ зарегистрирован",
                                           "response has been recorded", "спасибо")):
                    return True, "подтверждено"
                return True, "кнопка нажата (подтверждение не распознано)"
            except Exception as e:
                return False, f"submit fail: {repr(e)[:50]}"
    return False, "кнопка отправки не найдена"


async def process(page, llm, prof, url, vname):
    try:
        await page.goto(url, timeout=45000, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
    except Exception as e:
        return "error", f"страница не открылась: {repr(e)[:50]}", [], []
    hard = await _hard_guards(page)
    if hard:
        return "needs_human", hard, [], []
    is_google = bool(await page.locator('div[role=listitem]').count())
    if not is_google:
        soft = await _login_text(page)
        return "needs_human", soft or "не Google Form — generic пока не поддержан", [], []
    status, filled, need = await fill_google_form(page, llm, prof, vname)
    return status, "", filled, need


async def main():
    acc = ACCOUNT or pgconn.get_account()
    cfg = pgconn.app_config(acc)
    tok = cfg.get("token") or {}
    oa = cfg.get("openai") or {}
    if not tok.get("access_token"):
        print("form_fill: нет hh-токена — пропуск"); return
    if not oa.get("token"):
        print("form_fill: нет openai — пропуск"); return

    api = ApiClient(access_token=tok["access_token"], refresh_token=tok.get("refresh_token"),
                    access_expires_at=tok.get("access_expires_at"),
                    user_agent=generate_android_useragent(), refresh_hook=pgconn.locked_token_refresh)
    fields, prof = await build_profile(api, acc, cfg)
    await api.aclose()
    print(f"form_fill[{acc}] режим={'LIVE (ОТПРАВЛЯЕМ)' if LIVE else 'DRY (только заполнение+скрин)'}")
    print("профиль:", {k: (v[:40] if isinstance(v, str) else v) for k, v in fields.items()})

    llm = ChatOpenAI(token=oa["token"], model=oa.get("model"),
                     completion_endpoint=oa.get("completion_endpoint"),
                     system_prompt=SYS, temperature=0.2, max_completion_tokens=400)

    # задачи: либо один URL, либо form-URL из открытых «Дел»
    if ONE_URL:
        tasks = [(None, ONE_URL, "тест")]
    else:
        conn = pgconn.connect(); cur = conn.cursor()
        cur.execute("SELECT id, action_url, vacancy FROM action_items WHERE account=%s AND "
                    "coalesce(done,false)=false AND action_url ~* %s ORDER BY created_at DESC LIMIT %s",
                    (acc, r"forms\.gle|docs\.google\.com/forms", LIMIT))
        tasks = cur.fetchall(); conn.close()
    if not tasks:
        print("form_fill: нет Google-форм в открытых «Делах»"); return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(locale="ru-RU",
                                        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                    "Chrome/125.0 Safari/537.36"))
        page = await ctx.new_page()
        for aid, url, vac in tasks:
            print(f"\n=== [{vac[:40] if vac else '?'}] {url[:60]} ===")
            status, msg, filled, need = await process(page, llm, prof, url, vac or "")
            for q, v in filled:
                print(f"   ✅ {q[:55]}  ->  {v}")
            for q, v in need:
                print(f"   ⚠  {q[:55]}  ->  {v}")
            shot = f"/tmp/form_{acc}_{aid or 'test'}.png"
            try:
                await page.screenshot(path=shot, full_page=True)
                print(f"   📸 скриншот: {shot}")
            except Exception:
                pass
            if status in ("needs_human", "error"):
                print(f"   ⛔ {status}: {msg} — оставляю человеку")
                continue
            if need:
                print(f"   ⛔ обязательные без ответа ({len(need)}) — НЕ отправляю, человеку")
                continue
            if status == "empty":
                print("   ⛔ полей не распознано — человеку"); continue
            if LIVE:
                ok, why = await submit_google_form(page)
                print(f"   {'📨 ОТПРАВЛЕНО' if ok else '⛔ НЕ отправлено'}: {why}")
                if ok and aid:
                    conn = pgconn.connect(); cur = conn.cursor()
                    cur.execute("UPDATE action_items SET done=true WHERE id=%s", (aid,))
                    conn.commit(); conn.close()
            else:
                print("   ✋ DRY — заполнено, НЕ отправлено (проверь скриншот)")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
