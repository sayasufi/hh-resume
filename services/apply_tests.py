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
    return name or pgconn.get_account()


def tg_alert(text, priority=pgconn.PRIORITY_MED, key=None):
    """Кладёт алёрт в очередь уведомлений (отправит send_digest дайджестом).
    key -> dedup по дню, чтобы при повторных прогонах не спамить одним и тем же."""
    import datetime as dt

    dedup = (
        f"apply_tests:{key}:{dt.date.today().isoformat()}" if key else None
    )
    pgconn.notify(
        priority, f"apply_tests: {text}", category="apply_tests", dedup_key=dedup
    )


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


async def fill_textarea(page, sel, value) -> bool:
    """Устойчивое заполнение textarea. Сначала обычный page.fill (он сам скроллит
    и ждёт актуабельности). Если таймаутит (поле вне вида / под оверлеем / в
    React-форме) — выставляем значение через JS нативным сеттером + dispatch
    input/change, чтобы контролируемый React-компонент зарегистрировал ввод."""
    try:
        await page.fill(sel, value, timeout=6000)
        return True
    except Exception:
        pass
    try:
        return bool(await page.eval_on_selector(
            sel,
            """(el, val) => {
                if (!el) return false;
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value').set;
                setter.call(el, val);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }""",
            value,
        ))
    except Exception:
        return False


LETTER_SYS = (
    "Ты пишешь короткое сопроводительное письмо на русском от первого лица для "
    "отклика на вакансию на hh.ru. 3–5 предложений, по делу, без воды, клише и "
    "плейсхолдеров; не упоминай, что ты ИИ. Опирайся только на факты резюме. Без "
    "темы и заголовка — выводи только текст письма."
)


async def fill_cover_letter(page, letter_llm, vname):
    """Если на форме отклика есть тогл сопроводительного — раскрыть и заполнить
    коротким AI-письмом. Best-effort: ошибки НЕ блокируют отправку теста."""
    try:
        tog = await page.query_selector(
            "[data-qa=vacancy-response-letter-toggle]"
        )
        if not tog:
            return False  # на этой форме поля письма нет
        try:
            await tog.click(force=True)
            await page.wait_for_timeout(1200)
        except Exception:
            pass
        try:
            letter = (await letter_llm.send_message(
                f"Вакансия: {vname}. Напиши сопроводительное письмо."
            )).strip()
        except Exception:
            letter = ""
        if len(letter) < 20:  # LLM недоступна/плохой ответ -> короткий шаблон
            letter = (
                f"Здравствуйте! Заинтересовала ваша вакансия «{vname}». "
                "Мой опыт хорошо ложится на задачи, буду рад обсудить детали."
            )
        return await fill_textarea(
            page,
            'textarea[data-qa="vacancy-response-popup-form-letter-input"]',
            letter,
        )
    except Exception:
        return False


async def fill_task(page, task, llm, vname):
    """Вернёт (status, question, repr). status:
    'ok' — заполнено; 'manual' — форму авто-заполнить нельзя (помечаем seen);
    'transient' — временный сбой LLM/сети (НЕ помечаем seen, повторим)."""
    from hh_applicant_tool.ai.openai import OpenAIError

    q = task["question"] or "Ответьте на вопрос"
    if task["type"] == "text":
        # transient = РЕАЛЬНЫЙ сбой сети/LLM (OpenAIError). Если модель ответила,
        # но ответ плохой — повторяем раз, и если снова плохо → manual (не сеть
        # виновата, не надо крутить вечно).
        a = ""
        for attempt in (1, 2):
            try:
                a = (await llm.send_message(
                    f"Вакансия: {vname}\nВопрос: {q}\nОтветь кратко и по делу, без оговорок."
                )).strip()
            except OpenAIError as e:
                return "transient", q, f"LLM error: {repr(e)[:60]}"
            if not bad_answer(a):
                break
        if bad_answer(a):
            return "manual", q, f"LLM ответ ненадёжен: {a[:60]}"
        sel = f'textarea[name="{task["textarea"]}"]'
        if not await fill_textarea(page, sel, a):
            return "manual", q, "fill fail (textarea недоступна)"
        return "ok", q, a
    # radio / checkbox
    opts = task["options"]
    if not opts:
        return "manual", q, "(нет вариантов)"
    listing = "\n".join(f"{i + 1}) {o['label']}" for i, o in enumerate(opts))
    base = (
        f"Вакансия: {vname}\nВопрос: {q}\nВарианты:\n{listing}\n"
        f"Выбери ОДИН наиболее подходящий вариант исходя из резюме. Если в резюме "
        f"нет прямого ответа — выбери самый разумный/нейтрально-положительный "
        f"(например, согласие на формат работы). "
        f"Ответь СТРОГО одной цифрой от 1 до {len(opts)} и ничем больше."
    )
    # transient только при реальном сбое сети. Если модель «вильнула» и не дала
    # цифру — повторяем раз жёстче, потом manual (не крутим вечно, не считаем за
    # недоступность LLM).
    r = ""
    for attempt in (1, 2):
        try:
            r = (await llm.send_message(
                base if attempt == 1
                else base + "\n\nОтветь ТОЛЬКО числом, без слов и пояснений."
            )).strip()
        except OpenAIError as e:
            return "transient", q, f"LLM error: {repr(e)[:60]}"
        if re.search(r"\d+", r):
            break
    m = re.search(r"\d+", r)
    if not m:
        return "manual", q, f"LLM не дал номер: {r[:40]}"
    idx = int(m.group()) - 1
    if idx < 0 or idx >= len(opts):
        return "manual", q, f"номер вне диапазона: {r[:30]}"
    o = opts[idx]
    try:
        await page.click(f'input[name="{o["name"]}"][value="{o["value"]}"]', force=True, timeout=6000)
    except Exception as e:
        return "manual", q, f"click fail: {repr(e)[:50]}"
    return "ok", q, o["label"]


async def main():
    if not pgconn.feature_enabled("tests"):
        print("feat.tests выключен в Mini App — пропуск apply_tests")
        return
    global LIMIT
    if "--limit" not in sys.argv:  # лимит тестов = 25% от лимита авто-откликов (200 -> 50)
        _al = int(pgconn.get_setting("apply.max_per_day", 200) or 200)
        LIMIT = max(1, round(_al * 0.25))
    gph_only = bool(pgconn.get_setting("apply.civil_law_only", False))  # общий фильтр ГПХ
    cfg = pgconn.app_config()
    user, pw = creds()
    tok = cfg["token"]; oa = cfg["openai"]
    api = ApiClient(access_token=tok["access_token"], refresh_token=tok["refresh_token"],
                    access_expires_at=tok["access_expires_at"], user_agent=generate_android_useragent(),
                    refresh_hook=pgconn.locked_token_refresh)
    resume = (cfg.get("resume_text") or "").strip()
    salary = (cfg.get("preferences") or {}).get("salary")

    resume_id = pgconn.get_setting("apply.resume_id")
    if not resume_id:
        print("apply.resume_id не задан для схемы", pgconn.get_schema())
        await api.aclose()
        return

    # Город берём из hh-резюме (area.name — авторитетно), НЕ угадываем по тексту:
    # в resume_text может не быть текущего города, и LLM брал его из строки про вуз
    # и отвечал неверно (напр. «Волгоград», когда кандидат на самом деле в Москве).
    city = None
    try:
        city = ((await api.get(f"/resumes/{resume_id}")).get("area") or {}).get("name")
    except Exception as e:
        print("не удалось получить город из резюме:", repr(e)[:60])

    sysp = SYS_BASE
    if salary:
        sysp += f"\n\nЖелаемая зарплата кандидата: {salary}. На вопросы о зарплате/доходе указывай её."
    if city:
        sysp += (f"\n\nГород проживания кандидата: {city}. На вопросы о городе/локации "
                 f"указывай именно этот город (не выдумывай другой по строке про вуз). "
                 f"Кандидат физически находится в этом городе.")
    if resume:
        sysp += "\n\nРезюме:\n" + resume
    llm = ChatOpenAI(token=oa["token"], model=oa.get("model"), completion_endpoint=oa.get("completion_endpoint"),
                     system_prompt=sysp, temperature=0.3, max_completion_tokens=300)
    # отдельная LLM для сопроводительного письма (заполняем поле на форме отклика)
    letter_sys = LETTER_SYS + (("\n\nРезюме:\n" + resume) if resume else "")
    letter_llm = ChatOpenAI(token=oa["token"], model=oa.get("model"),
                            completion_endpoint=oa.get("completion_endpoint"),
                            system_prompt=letter_sys, temperature=0.5, max_completion_tokens=300)

    seen = pgconn.seen_keys("tests")

    try:
        r = await api.get(f"/resumes/{resume_id}/similar_vacancies", page=0, per_page=80)
    finally:
        await api.aclose()  # api больше не нужен — дальше только браузер
    tvs = [v for v in r.get("items", [])
           if v.get("has_test") and str(v["id"]) not in seen
           and (not gph_only or v.get("civil_law_contracts"))]
    print(f"test vacancies (new): {len(tvs)}" + (" [только ГПХ]" if gph_only else ""))
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
        # Логинимся, если нет сохранённой веб-сессии ИЛИ страница ушла на login/signup/account
        need_login = (
            not cfg.get("web_state")
            or any(x in page.url.lower() for x in ("login", "signup", "account", "auth"))
        )
        if need_login:
            print("веб-сессия отсутствует/невалидна -> логин")
            if not await web_login(page, user, pw):
                tg_alert("не удалось залогиниться в веб hh (возможно капча/OTP)", pgconn.PRIORITY_MED, key="login")
                await browser.close(); return
            pgconn.set_app_config("web_state", await ctx.storage_state())
            # после логина вернёмся на резюме, чтобы убедиться
            await page.goto("https://hh.ru/applicant/resumes", timeout=40000, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)

        def save_seen():
            if APPLY:
                pgconn.add_seen("tests", seen)

        done = 0
        transient_streak = 0  # подряд идущие блипы LLM/сети
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
                    print(f"[{vid}] задачи не распознаны -> пропуск (вручную)")
                    seen.add(str(vid)); done += 1; save_seen(); continue

                print(f"\n=== [{vid}] {vname} | задач: {len(tasks)} ===")
                statuses = []
                for t in tasks:
                    st, q, a = await fill_task(page, t, llm, vname)
                    print(f"  [{st}/{t['type']}] Q: {q[:70]}\n          A: {a}")
                    statuses.append(st)
                    if st != "ok":
                        break
                if "transient" in statuses:
                    # Один блип LLM/сети — пропускаем ЭТУ вакансию БЕЗ seen (вернётся
                    # в след. заход) и идём дальше, не рушим весь прогон. Прерываем
                    # только при серии блипов подряд (LLM реально недоступна).
                    transient_streak += 1
                    print(f"[{vid}] временный сбой (LLM/сеть) #{transient_streak} -> пропуск без seen")
                    if transient_streak >= 3:
                        print("3 временных сбоя подряд -> LLM/сеть недоступны, прогон прерван")
                        tg_alert("LLM/сеть недоступны, прогон прерван (вакансии не сожжены)", pgconn.PRIORITY_LOW, key="transient")
                        break
                    continue
                transient_streak = 0  # дошли без блипа — сбрасываем счётчик
                if "manual" in statuses:
                    print(f"[{vid}] форму нельзя авто-заполнить -> пропуск (вручную)")
                    seen.add(str(vid)); done += 1; save_seen(); continue

                _shot_dir = f"/tmp/{pgconn.get_schema()}"
                os.makedirs(_shot_dir, exist_ok=True)
                await page.screenshot(path=f"{_shot_dir}/test_filled_{vid}.png", full_page=True)
                # сопроводительное письмо (если на форме есть поле/тогл письма)
                if await fill_cover_letter(page, letter_llm, vname):
                    print("  ✍️ сопроводительное добавлено")
                if APPLY:
                    btn = await page.query_selector('button[data-qa="vacancy-response-submit-popup"], button[data-qa*="response-submit"]')
                    if btn:
                        await btn.click(); await page.wait_for_timeout(4000)
                        print(f"  -> ОТПРАВЛЕНО ({page.url})")
                        seen.add(str(vid)); done += 1; pgconn.bump_activity("tests", 1); save_seen()
                    else:
                        print("  кнопка отправки не найдена -> НЕ помечаю seen")
                else:
                    # DRY: ничего не отправляем и НЕ помечаем seen (save_seen и так
                    # no-op без APPLY); done++ только чтобы --limit работал в dry.
                    print("  DRY: не отправлено")
                    done += 1
            except Exception as e:
                # неожиданная ошибка — НЕ жжём вакансию, попробуем в след. раз
                print(f"[{vid}] неожиданная ошибка: {repr(e)[:120]} -> НЕ помечаю seen")
                continue

        await browser.close()
    print("done:", done)


if __name__ == "__main__":
    asyncio.run(main())
