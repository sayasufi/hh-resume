"""Авто-отклик на вакансии с тестом через веб (Playwright). Обрабатывает
текстовые вопросы, radio/checkbox (выбор) и миксы; пустой вопрос берёт из описания.

  python apply_tests.py [--apply] [--limit N]
    без --apply = dry (заполнить + скриншот, НЕ отправлять)
"""
import os
import re
import sys
import asyncio

from playwright.async_api import async_playwright
from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent
from hh_applicant_tool.ai import ChatOpenAI
from hh_applicant_tool.storage import pgconn

APPLY = "--apply" in sys.argv
LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 1

SYS_BASE = (
    "Ты помогаешь кандидату пройти тест при отклике на вакансию hh.ru. "
    "Отвечай ОТ ПЕРВОГО ЛИЦА, кратко, правдиво, опираясь на резюме ниже. "
    "Не приписывай себе опыт, которого нет в резюме (на честные да/нет отвечай честно). "
    "Отвечай только содержанием ответа, без преамбул."
)

# Достаём задачи теста в порядке DOM: вопрос -> его инпуты.
EXTRACT_JS = r"""
() => {
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  const desc = norm(document.querySelector('[data-qa="test-description"]')?.innerText);
  const nodes = [...document.querySelectorAll(
    '[data-qa="task-question"], input[type=radio][name^="task_"], input[type=checkbox][name^="task_"], textarea[name^="task_"]')];
  const tasks = []; let cur = null;
  for (const el of nodes) {
    if (el.getAttribute && el.getAttribute('data-qa') === 'task-question') {
      cur = {question: norm(el.innerText), type: null, options: [], textarea: null};
      tasks.push(cur);
    } else {
      if (!cur) { cur = {question:'', type:null, options:[], textarea:null}; tasks.push(cur); }
      if (el.tagName === 'TEXTAREA') { cur.type = 'text'; cur.textarea = el.name; }
      else {
        cur.type = el.type;
        const lbl = norm(el.closest('label')?.innerText) || norm(el.parentElement?.innerText);
        cur.options.push({name: el.name, value: el.value, label: lbl});
      }
    }
  }
  tasks.forEach(t => { if (!t.question) t.question = desc; });
  return {desc, tasks};
}
"""


def creds():
    return (
        pgconn.get_setting("auth.username"),
        pgconn.get_setting("auth.password"),
    )


def _user_label():
    try:
        name = pgconn.get_setting("user.full_name")
    except Exception:
        name = None
    return name or os.environ.get("HH_DB_SCHEMA", "public")


def tg_alert(cfg, text):
    import httpx

    t = cfg.get("telegram") or {}
    if t.get("token") and t.get("chat_id"):
        data = {"chat_id": t["chat_id"], "text": f"👤 {_user_label()}\n{text}"}
        if t.get("topic_id"):
            data["message_thread_id"] = t["topic_id"]
        try:
            httpx.post(
                f"https://api.telegram.org/bot{t['token']}/sendMessage",
                data=data, timeout=20,
            )
        except Exception:
            pass


def bad_answer(a):
    low = a.lower()
    return (len(a) < 2 or "предоставьте" in low or "пришлите" in low
            or "текст вопрос" in low or "уточните вопрос" in low or "сформулирую" in low)


async def web_login(page, user, pw):
    await page.goto("https://hh.ru/account/login", timeout=40000, wait_until="domcontentloaded")
    if not await page.query_selector('input[data-qa="credential-type-EMAIL"]'):
        sb = await page.query_selector('button[data-qa="submit-button"]')
        if sb:
            await sb.click(); await page.wait_for_timeout(3000)
    try:
        await page.click('input[data-qa="credential-type-EMAIL"]', force=True, timeout=6000)
        await page.wait_for_timeout(700)
    except Exception:
        pass
    for sel in ('input[data-qa="applicant-login-input-email"]', 'input[name="username"]'):
        if await page.query_selector(sel):
            await page.fill(sel, user); break
    try:
        await page.click('button[data-qa="expand-login-by-password"]', force=True, timeout=6000)
        await page.wait_for_timeout(1200)
    except Exception:
        pass
    for sel in ('input[data-qa="applicant-login-input-password"]', 'input[type="password"]'):
        if await page.query_selector(sel):
            await page.fill(sel, pw); break
    for sel in ('button[data-qa="account-login-submit"]', 'button[data-qa="submit-button"]', 'button[type="submit"]'):
        el = await page.query_selector(sel)
        if el:
            await el.click(); break
    await page.wait_for_timeout(6000)
    return "login" not in page.url.lower() and "otp" not in page.url.lower()


async def fill_task(page, task, llm, vname):
    """Вернёт (ok, question, answer_repr)."""
    q = task["question"] or "Ответьте на вопрос"
    if task["type"] == "text":
        a = (await llm.send_message(
            f"Вакансия: {vname}\nВопрос: {q}\nОтветь кратко."
        )).strip()
        if bad_answer(a):
            return False, q, a[:80]
        sel = f'textarea[name="{task["textarea"]}"]'
        try:
            el = await page.query_selector(sel)
            if el:
                await el.scroll_into_view_if_needed(timeout=4000)
            await page.fill(sel, a, timeout=8000)
        except Exception as e:
            return False, q, f"fill fail: {repr(e)[:50]}"
        return True, q, a
    # radio / checkbox
    opts = task["options"]
    if not opts:
        return False, q, "(нет вариантов)"
    listing = "\n".join(f"{i + 1}) {o['label']}" for i, o in enumerate(opts))
    r = (await llm.send_message(
        f"Вакансия: {vname}\nВопрос: {q}\nВарианты:\n{listing}\n"
        "Выбери ОДИН правдивый вариант (исходя из резюме). Ответь ТОЛЬКО номером варианта."
    )).strip()
    m = re.search(r"\d+", r)
    if not m:
        return False, q, r[:60]
    idx = int(m.group()) - 1
    if idx < 0 or idx >= len(opts):
        return False, q, f"номер вне диапазона: {r[:30]}"
    o = opts[idx]
    try:
        await page.click(f'input[name="{o["name"]}"][value="{o["value"]}"]', force=True, timeout=6000)
    except Exception as e:
        return False, q, f"click fail: {repr(e)[:50]}"
    return True, q, o["label"]


async def main():
    cfg = pgconn.app_config()
    user, pw = creds()
    tok = cfg["token"]; oa = cfg["openai"]
    api = ApiClient(access_token=tok["access_token"], refresh_token=tok["refresh_token"],
                    access_expires_at=tok["access_expires_at"], user_agent=generate_android_useragent())
    resume = (cfg.get("resume_text") or "").strip()
    salary = (cfg.get("preferences") or {}).get("salary")
    sysp = SYS_BASE
    if salary:
        sysp += f"\n\nЖелаемая зарплата кандидата: {salary}. На вопросы о зарплате/доходе указывай её."
    if resume:
        sysp += "\n\nРезюме:\n" + resume
    llm = ChatOpenAI(token=oa["token"], model=oa.get("model"), completion_endpoint=oa.get("completion_endpoint"),
                     system_prompt=sysp, temperature=0.3, max_completion_tokens=300)

    resume_id = pgconn.get_setting("apply.resume_id")
    if not resume_id:
        print("apply.resume_id не задан для схемы", pgconn.get_schema())
        return

    seen = pgconn.seen_keys("tests")

    r = await api.get(f"/resumes/{resume_id}/similar_vacancies", page=0, per_page=80)
    tvs = [v for v in r.get("items", []) if v.get("has_test") and str(v["id"]) not in seen]
    print(f"test vacancies (new): {len(tvs)}")
    if not tvs:
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            storage_state=cfg.get("web_state") or None,
            viewport={"width": 1280, "height": 900}, locale="ru-RU",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
        page = await ctx.new_page()
        await page.goto("https://hh.ru/applicant/resumes", timeout=40000, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        if "login" in page.url.lower() or "account" in page.url.lower():
            print("сессия невалидна -> логин")
            if not await web_login(page, user, pw):
                tg_alert(cfg, "⚠️ apply_tests: не удалось залогиниться в веб hh (возможно OTP).")
                await browser.close(); return
            pgconn.set_app_config("web_state", await ctx.storage_state())

        def save_seen():
            if APPLY:
                pgconn.add_seen("tests", seen)

        done = 0
        for v in tvs:
            if done >= LIMIT:
                break
            vid = v["id"]; vname = v.get("name", "")
            try:
                await page.goto(f"https://hh.ru/applicant/vacancy_response?vacancyId={vid}",
                                timeout=40000, wait_until="domcontentloaded")
                await page.wait_for_timeout(3500)
                if not await page.query_selector('input[name="testRequired"], [data-qa="task-question"], textarea[name^="task_"]'):
                    print(f"[{vid}] форма теста не найдена ({page.url})")
                    continue  # не помечаем seen — попробуем в след. раз
                data = await page.evaluate(EXTRACT_JS)
                tasks = data["tasks"]
                if not tasks:
                    print(f"[{vid}] задачи не распознаны -> пропуск")
                    seen.add(str(vid)); done += 1; save_seen(); continue

                print(f"\n=== [{vid}] {vname} | задач: {len(tasks)} ===")
                ok_all = True
                for t in tasks:
                    ok, q, a = await fill_task(page, t, llm, vname)
                    print(f"  [{'OK' if ok else 'FAIL'}/{t['type']}] Q: {q[:70]}\n          A: {a}")
                    if not ok:
                        ok_all = False; break
                if not ok_all:
                    print(f"[{vid}] не смог надёжно заполнить -> пропуск (вручную)")
                    seen.add(str(vid)); done += 1; save_seen(); continue

                _shot_dir = f"/tmp/{pgconn.get_schema()}"
                os.makedirs(_shot_dir, exist_ok=True)
                await page.screenshot(path=f"{_shot_dir}/test_filled_{vid}.png", full_page=True)
                if APPLY:
                    btn = await page.query_selector('button[data-qa="vacancy-response-submit-popup"], button[data-qa*="response-submit"]')
                    if btn:
                        await btn.click(); await page.wait_for_timeout(4000)
                        print(f"  -> ОТПРАВЛЕНО ({page.url})")
                    else:
                        print("  кнопка отправки не найдена")
                else:
                    print("  DRY: не отправлено")
                seen.add(str(vid)); done += 1; save_seen()
            except Exception as e:
                print(f"[{vid}] ошибка: {repr(e)[:120]} -> пропуск")
                seen.add(str(vid)); done += 1; save_seen()
                continue

        await browser.close()
    print("done:", done)


asyncio.run(main())
