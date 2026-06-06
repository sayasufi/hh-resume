"""Авто-заполнение веб-форм (анкеты/опросы по ссылке) через Playwright + LLM.
Обрабатывает «дела»-формы: открывает ссылку, извлекает вопросы, LLM отвечает по «карте
кандидата» (из резюме) и по открытым вопросам, заполняет, сабмитит.

ПОДДЕРЖКА: Google Forms (role=listitem) + простые кастомные формы (input/textarea/select).
СКИП (остаётся человеку): требуется логин (редирект на signin/login/auth), реальная капча
(g-recaptcha / grecaptcha), мультистеп/загрузка файла.

БЕЗОПАСНОСТЬ: DRY по умолчанию — извлекает, генерит ответы, ЗАПОЛНЯЕТ поля, но НЕ сабмитит
(нужен --live). Показывает каждую пару вопрос->ответ.
"""
import asyncio
import json
import re
import sys

import giga_recruiter as gr
from hh_applicant_tool.ai import ChatOpenAI
from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.storage import pgconn
from playwright.async_api import async_playwright

LIVE = "--live" in sys.argv
FILL = "--fill" in sys.argv      # заполнить поля, но НЕ сабмитить (валидация без отправки)
DO_FILL = LIVE or FILL
DRY = not DO_FILL
# маркеры «LLM не понял вопрос» — такие поля не заполняем; много таких -> форму не трогаем
CONFUSED = re.compile(r"не указан|не могу понять|не понятн|вероятно[, ]|технический сбой|"
                      r"поскольку в (вопрос|ваш)|уточните вопрос|нет конкретного вопрос", re.I)
HTTP = re.compile(r"https?://(?!t\.me)[^\s)]+")
FORM_KW = ("форм", "анкет", "опрос", "заявк", "google", "регистрац")
MAX_FORMS = 4


def _pending_forms(account):
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, action, action_url, vacancy, nid FROM action_items "
                "WHERE account=%s AND coalesce(done,false)=false AND nid IS NOT NULL "
                "ORDER BY created_at DESC", (account,))
            return [{"id": r[0], "action": r[1] or "", "action_url": r[2] or "",
                     "vac": r[3] or "", "nid": r[4]} for r in cur.fetchall()]
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


async def _last_employer_msg(api, nid):
    try:
        m = await api.get(f"/negotiations/{nid}/messages", page=0)
        if m.get("pages", 1) > 1:
            m = await api.get(f"/negotiations/{nid}/messages", page=m["pages"] - 1)
    except Exception:
        return ""
    emp = [x for x in (m.get("items") or [])
           if x.get("text") and x["author"]["participant_type"] == "employer"]
    return "\n".join(x["text"] for x in emp[-2:]) if emp else ""


async def _candidate_card(api, cfg):
    """Известные данные кандидата для прямого заполнения полей формы."""
    card = {"resume_text": (cfg.get("resume_text") or "").strip()}
    rid = pgconn.get_setting("apply.resume_id")
    try:
        r = await api.get(f"/resumes/{rid}") if rid else {}
    except Exception:
        r = {}
    fn, ln, mn = r.get("first_name", ""), r.get("last_name", ""), r.get("middle_name", "")
    card["ФИО"] = " ".join(x for x in (ln, fn, mn) if x) or gr._label()
    card["имя"] = fn or gr._label().split()[0]
    card["город"] = (r.get("area") or {}).get("name", "")
    sal = r.get("salary") or {}
    card["зарплата"] = str(sal.get("amount") or
                           (cfg.get("preferences") or {}).get("salary") or "")
    card["ссылка на резюме hh"] = r.get("alternate_url") or (f"https://hh.ru/resume/{rid}" if rid else "")
    bd = r.get("birth_date") or ""
    if bd[:4].isdigit():
        import datetime as _d
        try:
            card["возраст"] = str(_d.date.today().year - int(bd[:4]))
        except Exception:
            pass
    for c in (r.get("contact") or []):
        t = (c.get("type") or {}).get("id", "")
        v = c.get("value")
        if t == "email":
            card["email"] = v
        elif t == "cell" and isinstance(v, dict):
            card["телефон"] = v.get("formatted") or ""
    tgu = pgconn.get_setting("getmatch.username", "") or ""
    if tgu:
        card["telegram"] = "@" + tgu.lstrip("@")
    return {k: v for k, v in card.items() if v and k != "resume_text"}, card["resume_text"]


EXTRACT_JS = r"""() => {
  const gf = location.href.includes('docs.google.com/forms');
  const out = [];
  if (gf) {
    document.querySelectorAll('div[role=listitem]').forEach((it, i) => {
      const h = it.querySelector('div[role=heading]'); if (!h) return;
      const q = h.innerText.replace(/\*$/,'').trim();
      if (it.querySelector('input[type=text]')) out.push({i, q, t:'text'});
      else if (it.querySelector('textarea')) out.push({i, q, t:'textarea'});
      else if (it.querySelector('div[role=radio]')) {
        const opts=[...it.querySelectorAll('div[role=radio]')].map(o=>o.getAttribute('aria-label')||o.dataset.value||'').filter(Boolean);
        out.push({i, q, t:'radio', opts});
      } else if (it.querySelector('div[role=checkbox]')) {
        const opts=[...it.querySelectorAll('div[role=checkbox]')].map(o=>o.getAttribute('aria-label')||'').filter(Boolean);
        out.push({i, q, t:'checkbox', opts});
      }
    });
  } else {
    const T = el => ((el && (el.innerText||el.textContent))||'').trim().replace(/\s+/g,' ');
    const labelFor = (e) => {
      if (e.labels && e.labels[0] && T(e.labels[0])) return T(e.labels[0]);
      const lb = e.getAttribute('aria-labelledby');
      if (lb) { const x=document.getElementById(lb); if (x && T(x)) return T(x); }
      let node = e;                                  // ближайший вопрос-текст вверх по контейнерам
      for (let up=0; up<5 && node; up++) {
        node = node.parentElement; if (!node) continue;
        const c = node.querySelector('label,legend,h1,h2,h3,h4,h5,h6,[class*=label],[class*=title],[class*=quest]');
        if (c && !c.contains(e) && T(c).length>2) return T(c);
      }
      return e.getAttribute('aria-label') || e.placeholder || e.name || '';
    };
    const groups = {};
    document.querySelectorAll('input,textarea,select').forEach((e, i) => {
      const ty=(e.type||'').toLowerCase();
      if (['hidden','submit','button','file','reset','image'].includes(ty)) return;
      if ((ty==='radio'||ty==='checkbox') && e.name) {   // радио/чек одной группы -> один вопрос
        const opt = T(e.closest('label')) || e.getAttribute('aria-label') || e.value || '';
        if (groups[e.name]) { groups[e.name].opts.push(opt); return; }
        const g = {i, q: labelFor(e).slice(0,100), t: ty, name: e.name, opts: [opt]};
        groups[e.name] = g; out.push(g);
      } else {
        out.push({i, q: labelFor(e).slice(0,100), t: e.tagName.toLowerCase()+'/'+ty, name: e.name||''});
      }
    });
  }
  const html=document.body.innerHTML.toLowerCase(), txt=document.body.innerText.toLowerCase();
  return {url:location.href, gf, fields:out,
    captcha:/g-recaptcha|grecaptcha|hcaptcha|smart-captcha/.test(html),
    login:/accounts\.google\.com\/(v3\/)?signin|\/login|\/signin|\/auth\//.test(location.href)
      || /войдите, чтобы продолжить|необходимо войти|войти в аккаунт|sign in to continue/.test(txt)};
}"""


async def _answer_field(oa, card, resume, q, opts):
    sysp = (f"Ты — кандидат, заполняешь анкету-форму работодателя. Данные о тебе:\n{json.dumps(card, ensure_ascii=False)}\n"
            f"Твой опыт (резюме):\n{resume[:2500]}\n"
            "Отвечай ОТ ПЕРВОГО ЛИЦА, по делу, честно (не выдумывай навыки, которых нет). "
            "Если поле — ФИО/email/телефон/город/зарплата/ссылка — бери из данных выше. "
            "Открытый вопрос — короткий конкретный ответ (1-3 предложения).")
    if opts:
        sysp += f"\nЭто выбор из вариантов: {opts}. Ответь СТРОГО одним точным вариантом из списка."
    chat = ChatOpenAI(token=oa["token"], model=oa.get("model"),
                      completion_endpoint=oa.get("completion_endpoint"), system_prompt=sysp,
                      temperature=0.2, max_completion_tokens=200)
    return ((await chat.send_message(f"Вопрос формы: «{q}»")) or "").strip()


def _match_opt(opts, ans):
    a = (ans or "").strip().lower()
    return (next((o for o in opts if o and o.strip().lower() == a), None)
            or next((o for o in opts if o and (a in o.lower() or o.lower() in a)), None))


async def _fill_field(page, gf, f, ans):
    TO = 8000  # короткий таймаут на действие (не 30с по умолчанию)
    if gf:  # Google Forms — listitem по индексу, поля через role
        item = page.locator("div[role=listitem]").nth(f["i"])
        if f["t"] in ("text", "textarea"):
            # Google Forms paragraph -> textarea (role!=textbox), short -> input[type=text]
            box = item.locator("textarea, input[type=text]").first
            await box.fill(ans, timeout=TO)
        else:  # radio / checkbox — клик по варианту (scope = listitem -> верная группа)
            role = "radio" if f["t"] == "radio" else "checkbox"
            pick = _match_opt(f.get("opts") or [], ans)
            if pick:
                await item.get_by_role(role, name=re.compile(re.escape(pick[:25]), re.I)).first.click(timeout=TO)
    else:  # кастомная форма — по name
        base, sub = f["t"].split("/")[0], f["t"].split("/")[-1]
        if sub in ("radio", "checkbox") and f.get("name"):
            pick = _match_opt(f.get("opts") or [], ans)
            if pick:
                loc = page.get_by_role("radio" if sub == "radio" else "checkbox",
                                       name=re.compile(re.escape(pick[:20]), re.I))
                if await loc.count() == 0:  # многие формы — value=да/нет без доступного имени
                    loc = page.locator(f"input[name='{f['name']}']")
                await loc.first.check(timeout=TO)
        elif f.get("name") and base == "select":
            await page.locator(f"[name='{f['name']}']").first.select_option(label=ans, timeout=TO)
        elif f.get("name") and base in ("input", "textarea"):
            await page.locator(f"[name='{f['name']}']").first.fill(ans, timeout=TO)


async def _fill_form(page, oa, card, resume, do_fill):
    d = await page.evaluate(EXTRACT_JS)
    if d["login"]:
        return "login", 0
    if d["captcha"]:
        return "captcha", 0
    fields = [f for f in d["fields"] if f.get("q")]
    if not fields:
        return "no_fields", 0
    plan, confused = [], 0
    for f in fields:
        ans = await _answer_field(oa, card, resume, f["q"], f.get("opts") or [])
        bad = (not ans) or bool(CONFUSED.search(ans))
        confused += bad
        plan.append((f, ans, bad))
        print(f"    Q: «{f['q'][:72]}» [{f['t']}]\n      A: «{ans[:100]}»"
              + (" ⚠️путаница" if bad else ""))
    if confused >= max(2, (len(fields) + 2) // 3):  # метки плохие -> не рискуем мусором
        return "unreliable", confused
    if not do_fill:
        return "ready", len(fields) - confused
    filled = 0
    for f, ans, bad in plan:
        if bad or not ans:
            continue
        try:
            await _fill_field(page, d["gf"], f, ans)
            filled += 1
        except Exception as e:
            print(f"      (не заполнил «{f['q'][:28]}»: {type(e).__name__})")
    return "filled", filled


async def main():
    cfg = pgconn.app_config()
    account = pgconn.get_account()
    oa = cfg.get("openai") or {}
    if not oa.get("token") or not (cfg.get("token") or {}).get("access_token"):
        print("form_fill: нет openai/hh — пропуск")
        return
    api = ApiClient(access_token=cfg["token"]["access_token"], refresh_token=cfg["token"]["refresh_token"],
                    access_expires_at=cfg["token"]["access_expires_at"],
                    user_agent=generate_android_useragent(), refresh_hook=pgconn.locked_token_refresh)
    card, resume = await _candidate_card(api, cfg)
    mode = "LIVE(сабмит)" if LIVE else ("FILL(без сабмита)" if FILL else "DRY")
    print(f"form_fill[{account}] {mode} | карта: {list(card)}")
    forms = []
    for t in _pending_forms(account):
        low = t["action"].lower()
        if not any(k in low for k in FORM_KW):
            continue
        msg = await _last_employer_msg(api, t["nid"])
        m = HTTP.search((t["action_url"] or "") + " " + msg)
        if m:
            forms.append((m.group(0).rstrip(".,"), t))
        if len(forms) >= MAX_FORMS:
            break
    await api.aclose()
    print(f"form_fill: форм-дел: {len(forms)}")
    if not forms:
        return
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for url, t in forms:
            print(f"\n  [ФОРМА] дело #{t['id']} «{t['vac'][:38]}»\n  {url[:75]}")
            try:
                ctx = await browser.new_context(locale="ru-RU")
                page = await ctx.new_page()
                await page.goto(url, timeout=35000, wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)
                status, n = await _fill_form(page, oa, card, resume, DO_FILL)
                if status == "login":
                    print("    -> требует логин — пропуск (остаётся человеку)")
                elif status == "captcha":
                    print("    -> капча — пропуск (остаётся человеку)")
                elif status == "no_fields":
                    print("    -> поля не распознал — пропуск")
                elif status == "unreliable":
                    print(f"    -> метки формы непонятны ({n} путаниц) — пропуск (риск мусора, человеку)")
                elif status == "ready":
                    print(f"    -> готов заполнить {n} полей (DRY — без заполнения)")
                else:  # filled
                    print(f"    -> ЗАПОЛНЕНО полей: {n}")
                    if not LIVE:
                        print("    -> FILL-режим: НЕ сабмитил (валидация)")
                    elif n:
                        for sel in ('div[role=button][aria-label*="Отправить"]',
                                    'div[role=button]:has-text("Отправить")', 'button[type=submit]',
                                    'button:has-text("Отправить")', 'button:has-text("Submit")'):
                            try:
                                await page.locator(sel).first.click(timeout=4000)
                                print("    -> ОТПРАВЛЕНО")
                                _mark_done(t["id"])
                                break
                            except Exception:
                                continue
                await page.close()
            except Exception as e:
                print(f"    ОШИБКА: {type(e).__name__}: {str(e)[:90]}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
