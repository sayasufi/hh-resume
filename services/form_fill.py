"""form_fill.py — агент заполнения ВНЕШНИХ анкет/форм из «Дел» (action_items).

Браузер (Playwright/Chromium). Универсально: Google Forms, Yandex Forms и обычные
HTML/ARIA-формы (нативные input/textarea/select + radio/checkbox + role=radiogroup).
Многостраничные формы проходит целиком (жмёт «Далее», на последней — «Отправить»).
Ответы — из профиля кандидата (hh /me + активное резюме + resume_text + @telegram
из Telethon-сессии) через LLM.

ПРЕДОХРАНИТЕЛИ:
  • DRY по умолчанию — заполняет и делает скриншот, НЕ отправляет (--live чтобы слать);
  • капча / логин с паролем / загрузка файла / оплата -> человеку;
  • не-формы (видео-интервью, ATS с регистрацией, мессенджеры, VK/YouTube) -> человеку;
  • обязательное поле без ответа из профиля -> НЕ отправляем;
  • навыки-самооценки (по шкале/звёзды) -> максимум; «сколько лет опыта» не трогаем.

Запуск:
  python form_fill.py --account lexa --url <URL>            # один тест (dry)
  python form_fill.py --account lexa --url <URL> --inspect   # только показать поля
  python form_fill.py --account lexa [--limit N] [--live]    # из «Дел»
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
INSPECT = "--inspect" in sys.argv
LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 5
ONE_URL = sys.argv[sys.argv.index("--url") + 1] if "--url" in sys.argv else None
ACCOUNT = sys.argv[sys.argv.index("--account") + 1] if "--account" in sys.argv else None

SKIP_TOKEN = "ПРОПУСК"
MAX_STEPS = 10  # потолок страниц многостраничной формы

# «Дела» со ссылкой на форму, которые пробуем (в режиме из БД)
DB_FORM_RE = r"forms\.gle|docs\.google\.com/forms|forms\.yandex|/forms/|typeform|tally\.so|" \
             r"notion\.so|notion\.site|webask\.io|clck\.ru|ya\.cc"
# хосты/пути, которые точно НЕ авто-заполняемая форма -> сразу человеку
HUMAN_HOSTS = re.compile(
    r"brainhire|getprofi|xeniaai|/interview|vk\.com|youtu|myworkdayjobs|huntflow|"
    r"\.offer-job\.|t\.me/|disk\.|drive\.google|\.pdf($|\?)", re.I)
_CAPTCHA_SEL = ("iframe[src*=recaptcha]", "iframe[src*=hcaptcha]", "iframe[title*=recaptcha]",
                "div.g-recaptcha", "[class*=captcha]", "[id*=captcha]")

_NEXT_RE = re.compile(r"дал(ее|ьше)|продолж|next|вперёд|перейти к|дальше", re.I)
_SUBMIT_RE = re.compile(r"отправ|заверш|готов|submit|send|finish|подтверд|complete", re.I)

_RATING_Q = re.compile(r"оцен|по\s*\d*[- ]*балл|шкал|уровень\s+владени|насколько\s+хорошо|"
                       r"звёзд|звезд|рейтинг", re.I)
_YEARS_Q = re.compile(r"сколько\s+лет|стаж|возраст|лет\s+опыт", re.I)

SYS = (
    "Ты помогаешь кандидату заполнить анкету/форму при отклике на вакансию. "
    "Отвечай ОТ ПЕРВОГО ЛИЦА, кратко и ПРАВДИВО, строго по профилю ниже. "
    "НЕ приписывай кандидату опыт/навыки/контакты, которых нет в профиле. "
    "Если в профиле нет данных для честного ответа — ответь РОВНО одним словом: "
    f"{SKIP_TOKEN}. Без преамбул, только сам ответ."
)

# Универсальный сборщик полей: помечает контролы data-ff, возвращает список вопросов.
EXTRACT_JS = r"""
() => {
  const norm = s => (s||'').replace(/\s+/g,' ').trim().slice(0,300);
  const vis = el => { try { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
      return r.width>1 && r.height>1 && s.visibility!=='hidden' && s.display!=='none'; } catch(e){ return false; } };
  const esc = s => (window.CSS && CSS.escape) ? CSS.escape(s) : s;
  const FILLER = /^(мой ответ|your answer|введите( ответ| текст)?|enter( your answer)?|текст ответа|ответ|choose|выберите|select|—|-|\*)?$/i;
  const good = s => s && (s.length > 1 || /^\d$/.test(s.trim())) && !FILLER.test(s.trim());
  const label = el => {
    let v = norm((el.getAttribute && el.getAttribute('aria-label')) || '');
    if (good(v)) return v;
    const lb = el.getAttribute && el.getAttribute('aria-labelledby');
    if (lb) { v = norm(lb.split(/\s+/).map(id => { const e = document.getElementById(id); return e ? e.innerText : ''; }).join(' ')); if (good(v)) return v; }
    if (el.id) { const l = document.querySelector('label[for="'+esc(el.id)+'"]'); if (l) { v = norm(l.innerText); if (good(v)) return v; } }
    const wl = el.closest && el.closest('label'); if (wl) { v = norm(wl.innerText); if (good(v)) return v; }
    let n = el;
    for (let i=0; i<7 && n; i++, n=n.parentElement) {
      const hs = n.querySelectorAll ? n.querySelectorAll(
        'legend,[role=heading],h1,h2,h3,h4,h5,label,[class*=quest],[class*=Quest],[class*=title],[class*=Title],[class*=label],[class*=Label],[class*=question]') : [];
      for (const h of hs) { if (!h.contains(el)) { v = norm(h.innerText); if (good(v)) return v; } }
    }
    v = norm(el.placeholder || '');
    return good(v) ? v : '';
  };
  let k = 0;
  const tag = el => { if (!el.getAttribute('data-ff')) el.setAttribute('data-ff','ff'+(k++)); return el.getAttribute('data-ff'); };
  const req = el => {
    if (el.required || el.getAttribute('aria-required')==='true') return true;
    let n=el; for (let i=0;i<5&&n;i++,n=n.parentElement){ const a=(n.getAttribute&&(n.getAttribute('aria-label')||''))||'';
      if (/обязательн|required/i.test(a)) return true; if (n.querySelector && n.querySelector('[aria-label*="бязательн"],[aria-label*="equired"]')) return true; }
    return false;
  };
  const out = [];
  // текстовые
  document.querySelectorAll('input,textarea,[contenteditable=true],[role=textbox]').forEach(el => {
    if (!vis(el)) return;
    const tp = (el.getAttribute('type')||el.tagName).toLowerCase();
    if (['hidden','submit','button','reset','image','file','password','checkbox','radio','range'].includes(tp)) return;
    const long = el.tagName==='TEXTAREA' || el.getAttribute('contenteditable')==='true' || el.getAttribute('role')==='textbox';
    out.push({ff: tag(el), q: label(el), type: long?'long':'short', options: [], required: req(el)});
  });
  // select
  document.querySelectorAll('select').forEach(el => { if (!vis(el)) return;
    out.push({ff: tag(el), q: label(el), type: 'select', required: req(el),
      options: [...el.options].map(o => norm(o.textContent)).filter(Boolean)}); });
  // нативные radio по name
  const rg = {};
  document.querySelectorAll('input[type=radio]').forEach(el => { if (!vis(el)) return; const key = el.name || ('r'+tag(el)); (rg[key]=rg[key]||[]).push(el); });
  Object.values(rg).forEach(g => out.push({q: label(g[0]), type: 'radio', required: req(g[0]),
    options: g.map(el => ({ff: tag(el), label: label(el)}))}));
  // нативные checkbox по name
  const cg = {};
  document.querySelectorAll('input[type=checkbox]').forEach(el => { if (!vis(el)) return; const key = el.name || ('c'+tag(el)); (cg[key]=cg[key]||[]).push(el); });
  Object.values(cg).forEach(g => out.push({q: label(g[0]), type: 'checkbox', required: req(g[0]),
    options: g.map(el => ({ff: tag(el), label: label(el)}))}));
  // ARIA radiogroup
  const doneR = new Set();
  document.querySelectorAll('[role=radiogroup]').forEach(grp => { if (!vis(grp)) return;
    const rs = [...grp.querySelectorAll('[role=radio]')].filter(vis); if (!rs.length) return;
    rs.forEach(r => doneR.add(r));
    out.push({q: label(grp), type: 'radio', required: req(grp), options: rs.map(r => ({ff: tag(r), label: label(r)}))}); });
  // ARIA radio-сироты (Google listitem без radiogroup) — группируем по контейнеру
  const orphan = {};
  document.querySelectorAll('[role=radio]').forEach(r => { if (!vis(r)||doneR.has(r)) return;
    const cont = r.closest('[role=listitem],[role=group],fieldset,form') || document.body;
    if (!cont.getAttribute('data-ffg')) cont.setAttribute('data-ffg','g'+(k++));
    const key = cont.getAttribute('data-ffg'); (orphan[key]=orphan[key]||{cont, items:[]}).items.push(r); });
  Object.values(orphan).forEach(o => out.push({q: label(o.cont)||label(o.items[0]), type: 'radio', required: req(o.cont),
    options: o.items.map(r => ({ff: tag(r), label: label(r)}))}));
  // ARIA checkbox
  const oc = {};
  document.querySelectorAll('[role=checkbox]').forEach(c => { if (!vis(c)) return;
    const cont = c.closest('[role=listitem],[role=group],fieldset,form') || document.body;
    if (!cont.getAttribute('data-ffc')) cont.setAttribute('data-ffc','gc'+(k++));
    const key = cont.getAttribute('data-ffc'); (oc[key]=oc[key]||{cont, items:[]}).items.push(c); });
  Object.values(oc).forEach(o => out.push({q: label(o.cont)||'', type: 'checkbox', required: req(o.cont),
    options: o.items.map(c => ({ff: tag(c), label: label(c)}))}));
  return out.filter(o => (o.q && o.q.length>1) || (o.options && o.options.length));
}
"""


def _bad(a: str) -> bool:
    low = (a or "").lower()
    return (len((a or "").strip()) < 1 or "предоставьте" in low or "уточните" in low
            or "не могу" in low or "сформулир" in low)


def _rating_max_idx(q, options):
    if not _RATING_Q.search(q or "") or _YEARS_Q.search(q or ""):
        return None
    nums = [int(m.group(1)) if (m := re.match(r"^\s*(\d+)", o or "")) else None for o in options]
    if len(nums) >= 3 and all(n is not None for n in nums):
        return nums.index(max(nums))
    return None


async def build_profile(api, acc, cfg):
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
        if r.get("skill_set"):
            fields["Ключевые навыки"] = ", ".join(r["skill_set"][:20])
        exp = r.get("experience") or []
        if exp:
            fields["Текущее место"] = f"{exp[0].get('position','')} @ {exp[0].get('company','')}"
        _sch = r.get("schedule") or {}
        if isinstance(_sch, list):
            _sch = ", ".join((s.get("name") or "") for s in _sch if isinstance(s, dict))
        elif isinstance(_sch, dict):
            _sch = _sch.get("name") or ""
        fields["Формат работы"] = _sch or "удалённый/гибрид"
        fields["Резюме на hh"] = r.get("alternate_url") or ""
        bd = r.get("birth_date")
        if bd:
            try:
                from datetime import date
                y, m, d = (int(x) for x in bd[:10].split("-"))
                t = date.today()
                fields["Возраст"] = str(t.year - y - ((t.month, t.day) < (m, d)))
            except Exception:
                pass
    prefs = cfg.get("preferences") or {}
    if prefs.get("salary") and not fields.get("Желаемая зарплата"):
        fields["Желаемая зарплата"] = f"{prefs['salary']} рублей на руки"
    enc = cfg.get("tg_user_session")
    if enc:
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            aid, ah = pgconn.tg_api()
            c = TelegramClient(StringSession(pgconn.dec_session(enc)), aid, ah)
            await c.connect()
            if await c.is_user_authorized():
                me2 = await c.get_me()
                if me2 and me2.username:
                    fields["Телеграм"] = f"https://t.me/{me2.username}"
            await c.disconnect()
        except Exception:
            pass
    prof = "ПРОФИЛЬ КАНДИДАТА:\n" + "\n".join(f"- {k}: {v}" for k, v in fields.items() if v)
    if resume_txt:
        prof += "\n\nПОЛНОЕ РЕЗЮМЕ:\n" + resume_txt[:2500]
    return fields, prof


async def _answer_text(llm, prof, q, long=False):
    hint = "Развёрнуто (2-4 предложения)" if long else "Кратко, одной строкой"
    guide = ("Если вопрос ОТКРЫТЫЙ (почему вы / расскажите / мотивация / что можете дать) — "
             "ОБЯЗАТЕЛЬНО ответь по резюме и здравому смыслу, НЕ пропускай. "
             f"{SKIP_TOKEN} — ТОЛЬКО если просят конкретные данные, которых нет в профиле "
             "(телефон, ссылка на github/портфолио, номер, сертификат).")
    a = ""
    for attempt in (1, 2):
        try:
            a = (await llm.send_message(f"{prof}\n\nВопрос анкеты: {q}\n{hint}. {guide}")).strip()
        except OpenAIError as e:
            return None, f"LLM error: {repr(e)[:50]}"
        if a.upper().strip(".! ") == SKIP_TOKEN:
            return SKIP_TOKEN, "нет данных в профиле"
        if not _bad(a):
            return a, "ok"
    return None, f"ненадёжный ответ: {a[:50]}"


async def _answer_choice(llm, prof, q, options, multi=False):
    listing = "\n".join(f"{i+1}) {o}" for i, o in enumerate(options))
    rule = ("Можно несколько — номера через запятую." if multi else f"Ответь СТРОГО цифрой 1..{len(options)}.")
    base = (f"{prof}\n\nВопрос: {q}\nВарианты:\n{listing}\nВыбери по профилю. Нет прямого — "
            f"самый разумный (например, согласие на формат/переезд, если адекватно). {rule} "
            f"Если ни один не подходит и это исказит правду — {SKIP_TOKEN}.")
    r = ""
    for attempt in (1, 2):
        try:
            r = (await llm.send_message(base if attempt == 1
                 else base + f"\n\nОтветь ТОЛЬКО {'номерами' if multi else 'числом'}.")).strip()
        except OpenAIError as e:
            return None, f"LLM error: {repr(e)[:50]}"
        if SKIP_TOKEN in r.upper():
            return SKIP_TOKEN, "нет подходящего варианта"
        nums = [n - 1 for x in re.findall(r"\d+", r) if 0 <= (n := int(x)) - 1 < len(options)]
        if nums:
            return (nums if multi else nums[:1]), "ok"
    return None, f"LLM не дал номер: {r[:40]}"


async def _fill_text(page, ff, value):
    sel = f'[data-ff="{ff}"]'
    try:
        await page.fill(sel, value, timeout=5000)
        return True
    except Exception:
        pass
    try:  # contenteditable / капризный React
        return bool(await page.eval_on_selector(sel, """(el, val) => {
            if (el.isContentEditable) { el.focus(); el.textContent = val;
                el.dispatchEvent(new Event('input', {bubbles:true})); return true; }
            const proto = el.tagName==='TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, val);
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
            return true; }""", value))
    except Exception:
        return False


async def _click(page, ff):
    sel = f'[data-ff="{ff}"]'
    try:
        await page.click(sel, timeout=5000)
        return True
    except Exception:
        try:  # скрытый нативный input -> кликаем через JS
            await page.eval_on_selector(sel, "el => (el.closest('label')||el).click()")
            return True
        except Exception:
            return False


async def fill_page(page, llm, prof):
    """Заполнить текущую страницу/шаг. -> (filled:list, need:list, n_fields)."""
    try:
        fields = await page.evaluate(EXTRACT_JS)
    except Exception as e:
        return [], [], 0, f"extract error: {repr(e)[:50]}"
    filled, need = [], []
    for f in fields:
        q, typ, reqd = (f.get("q") or "").strip(), f.get("type"), f.get("required")
        opts_o = f.get("options") or []
        opts = [o.get("label") or f"вариант {i+1}" for i, o in enumerate(opts_o)]
        if typ in ("short", "long"):
            ans, why = await _answer_text(llm, prof, q or "Ответьте", long=(typ == "long"))
            if ans in (None, SKIP_TOKEN):
                (need if reqd else filled).append((q, f"пропуск ({why})")); continue
            filled.append((q, f"✎ {ans[:60]}")) if await _fill_text(page, f["ff"], ans) \
                else need.append((q, "fill не удался"))
        elif typ == "select":
            ridx = _rating_max_idx(q, opts)
            if ridx is None:
                ans, why = await _answer_choice(llm, prof, q or "Выберите", opts)
                if ans in (None, SKIP_TOKEN):
                    (need if reqd else filled).append((q, f"пропуск ({why})")); continue
                ridx = ans[0]
            try:
                await page.select_option(f'[data-ff="{f["ff"]}"]', index=ridx, timeout=5000)
                filled.append((q, f"▼ {opts[ridx]}"))
            except Exception as e:
                need.append((q, f"select fail: {repr(e)[:40]}"))
        elif typ in ("radio", "checkbox"):
            if not opts_o:
                if reqd:
                    need.append((q, "варианты не распознаны"))
                continue
            ridx = _rating_max_idx(q, opts) if typ == "radio" else None
            if ridx is not None:
                filled.append((q, f"○ {opts[ridx]} (навык→макс)")) if await _click(page, opts_o[ridx]["ff"]) \
                    else need.append((q, "клик rating не удался"))
                continue
            ans, why = await _answer_choice(llm, prof, q or "Выберите", opts, multi=(typ == "checkbox"))
            if ans in (None, SKIP_TOKEN):
                # чекбокс «не выбрал вариант» — норма (выбираешь что применимо), не блокер;
                # radio без ответа на обязательный вопрос — блокер.
                dest = filled if typ == "checkbox" else (need if reqd else filled)
                dest.append((q, f"пропуск ({why})")); continue
            picked = []
            for idx in (ans if typ == "checkbox" else ans[:1]):
                if await _click(page, opts_o[idx]["ff"]):
                    picked.append(opts[idx])
            filled.append((q, ("☑ " if typ == "checkbox" else "○ ") + "; ".join(picked)))
    return filled, need, len(fields), ""


async def _hard_guard(page):
    for sel in _CAPTCHA_SEL:
        if await page.query_selector(sel):
            return "капча"
    if await page.query_selector('input[type=password]'):
        return "форма требует логин (пароль)"
    if await page.query_selector('input[type=file][required], input[type=file]:not([multiple])'):
        # файл терпим, если не обязателен; грубо: наличие file-инпута отметим, но не блокируем текст-формы
        pass
    return None


async def _find_button(page, rx):
    for getter in (lambda: page.get_by_role("button", name=rx),
                   lambda: page.locator("button", has_text=rx),
                   lambda: page.locator('input[type=submit]')):
        try:
            loc = getter()
            if await loc.count():
                el = loc.first
                if rx is _SUBMIT_RE and (await el.get_attribute("type")) == "submit":
                    return el
                if await el.is_visible():
                    return el
        except Exception:
            continue
    return None


_FIELD_Q = ("()=>document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]),"
            "textarea,select,[role=radio],[role=checkbox],[role=textbox]').length")


async def _form_frame(page):
    """Документ ИЛИ iframe, где реально есть поля формы (webask и пр. рендерят в iframe)."""
    try:
        if await page.evaluate(_FIELD_Q):
            return page
    except Exception:
        pass
    for fr in page.frames[1:]:
        try:
            if await fr.evaluate(_FIELD_Q):
                return fr
        except Exception:
            continue
    return page


async def process(page, llm, prof, url, live):
    if HUMAN_HOSTS.search(url or ""):
        return "needs_human", "не авто-форма (интервью/видео/ATS/мессенджер/файл)", [], []
    try:
        await page.goto(url, timeout=45000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
    except Exception as e:
        return "error", f"страница не открылась: {repr(e)[:50]}", [], []
    if HUMAN_HOSTS.search(page.url):  # после редиректа сокращателя
        return "needs_human", "редирект на не-форму/ATS", [], []
    hard = await _hard_guard(page)
    if hard:
        return "needs_human", hard, [], []

    # SPA / интро-секция / опрос за кнопкой: пока полей нет — жмём старт/«Далее» и ждём
    _START = re.compile(r"начать|пройти|start|приступить|заполнить|откликнуться|"
                        r"дал(ее|ьше)|продолж|next|поехали|begin", re.I)
    for _ in range(3):
        if await _form_frame(page) is not page:
            break  # поля нашлись в iframe
        try:
            if await page.evaluate(_FIELD_Q):
                break
            btn = await _find_button(page, _START)
            if not btn:
                break
            await btn.click(timeout=6000)
            await page.wait_for_timeout(3500)
        except Exception:
            break

    # стена логина (напр. Google Forms только для авторизованных)
    frame0 = await _form_frame(page)
    if frame0 is page:
        try:
            has = await page.evaluate(_FIELD_Q)
        except Exception:
            has = 0
        if not has:
            try:
                body = (await page.inner_text("body"))[:3000].lower()
            except Exception:
                body = ""
            if any(w in body for w in ("войдите в аккаунт", "sign in", "чтобы заполнить эту форму",
                                       "необходимо войти", "требуется вход")):
                return "needs_human", "форма только для авторизованных (нужен вход в аккаунт)", [], []

    all_filled, all_need = [], []
    for step in range(MAX_STEPS):
        frame = await _form_frame(page)
        filled, need, nf, err = await fill_page(frame, llm, prof)
        all_filled += filled
        all_need += need
        if err:
            return "error", err, all_filled, all_need
        if step == 0 and nf == 0:
            return "needs_human", "полей формы не найдено (SPA/нестандартная)", all_filled, all_need
        if need:  # есть обязательные без ответа -> не листаем и не отправляем
            return "partial", "обязательные без ответа", all_filled, all_need
        nxt = await _find_button(frame, _NEXT_RE)
        sub = await _find_button(frame, _SUBMIT_RE)
        if sub and not nxt:  # последняя страница
            if not live:
                return "ready", "готово к отправке (dry)", all_filled, all_need
            try:
                await sub.click(timeout=6000)
                await page.wait_for_timeout(2500)
                body = (await page.inner_text("body")).lower()
                ok = any(w in body for w in ("ответ записан", "ответ зарегистр", "спасибо",
                                             "response has been recorded", "принят", "благодар"))
                return "submitted", ("подтверждено" if ok else "кнопка нажата"), all_filled, all_need
            except Exception as e:
                return "error", f"submit fail: {repr(e)[:50]}", all_filled, all_need
        if nxt:  # промежуточная страница -> дальше
            try:
                await nxt.click(timeout=6000)
                await page.wait_for_timeout(2200)
                continue
            except Exception:
                return "partial", "не смог перейти на след. страницу", all_filled, all_need
        # ни next, ни submit — одностраничная, заполнена
        return ("ready" if not live else "no_submit_btn",
                "заполнено, кнопки отправки нет" if live else "готово к отправке (dry)",
                all_filled, all_need)
    return "partial", "слишком много страниц", all_filled, all_need


async def inspect(page, url):
    try:
        await page.goto(url, timeout=40000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
    except Exception as e:
        print("   goto:", type(e).__name__, str(e)[:60]); return
    print("   итоговый URL:", page.url[:85])
    print("   human-host:", bool(HUMAN_HOSTS.search(page.url)))
    frame = await _form_frame(page)
    if frame is not page:
        print("   поля в IFRAME:", frame.url[:60])
    try:
        fields = await frame.evaluate(EXTRACT_JS)
    except Exception as e:
        print("   extract err:", repr(e)[:60]); return
    print(f"   полей найдено: {len(fields)}")
    for f in fields[:25]:
        opts = " | ".join((o.get("label") or "?")[:22] for o in (f.get("options") or [])[:6])
        print(f"     [{f.get('type')}{'*' if f.get('required') else ''}] {(f.get('q') or '?')[:55]}"
              + (f"  ({opts})" if opts else ""))


async def main():
    acc = ACCOUNT or pgconn.get_account()
    cfg = pgconn.app_config(acc)
    tok = cfg.get("token") or {}
    oa = cfg.get("openai") or {}
    if not tok.get("access_token"):
        print("form_fill: нет hh-токена — пропуск"); return

    api = ApiClient(access_token=tok["access_token"], refresh_token=tok.get("refresh_token"),
                    access_expires_at=tok.get("access_expires_at"),
                    user_agent=generate_android_useragent(), refresh_hook=pgconn.locked_token_refresh)
    fields, prof = await build_profile(api, acc, cfg)
    await api.aclose()
    print(f"form_fill[{acc}] режим={'INSPECT' if INSPECT else ('LIVE' if LIVE else 'DRY')}")
    if not INSPECT:
        print("профиль:", {k: (v[:38] if isinstance(v, str) else v) for k, v in fields.items()})

    llm = None
    if not INSPECT:
        if not oa.get("token"):
            print("form_fill: нет openai — пропуск"); return
        llm = ChatOpenAI(token=oa["token"], model=oa.get("model"),
                         completion_endpoint=oa.get("completion_endpoint"),
                         system_prompt=SYS, temperature=0.2, max_completion_tokens=400)

    if ONE_URL:
        tasks = [(None, ONE_URL, "тест")]
    else:
        conn = pgconn.connect(); cur = conn.cursor()
        cur.execute("SELECT id, action_url, vacancy FROM action_items WHERE account=%s AND "
                    "coalesce(done,false)=false AND action_url ~* %s ORDER BY created_at DESC LIMIT %s",
                    (acc, DB_FORM_RE, LIMIT))
        tasks = cur.fetchall(); conn.close()
    if not tasks:
        print("form_fill: нет form-URL в открытых «Делах»"); return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(locale="ru-RU", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"))
        page = await ctx.new_page()
        for aid, url, vac in tasks:
            print(f"\n=== [{(vac or '?')[:38]}] {url[:62]} ===")
            if INSPECT:
                await inspect(page, url); continue
            status, msg, filled, need = await process(page, llm, prof, url, LIVE)
            for q, v in filled:
                print(f"   ✅ {(q or '?')[:52]}  ->  {v}")
            for q, v in need:
                print(f"   ⚠  {(q or '?')[:52]}  ->  {v}")
            try:
                await page.screenshot(path=f"/tmp/form_{acc}_{aid or 'test'}.png", full_page=True)
            except Exception:
                pass
            print(f"   [{status}] {msg}")
            if status == "submitted" and aid:
                conn = pgconn.connect(); cur = conn.cursor()
                cur.execute("UPDATE action_items SET done=true WHERE id=%s", (aid,))
                conn.commit(); conn.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
