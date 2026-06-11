"""Выставить статус поиска резюме = «Активно ищу работу» (Playwright, веб hh).
Идемпотентно. Работает на сохранённой web_state-сессии (как apply_tests); пароль —
только фолбэк, если сессия протухла. Per-account (HH_ACCOUNT).
  python set_search_status.py             # выставить статус
  python set_search_status.py --discover  # только дамп DOM + скриншот
"""
import asyncio
import sys

from playwright.async_api import async_playwright
from hh_applicant_tool.storage import pgconn

TARGET = "Активно ищу работу"
DISCOVER = "--discover" in sys.argv


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


async def dismiss_overlays(page):
    """Закрыть промо/модалки, перекрывающие клики (magritte-modal-overlay)."""
    for _ in range(3):
        ov = await page.query_selector('[data-qa="modal-overlay"]')
        if not ov:
            return True
        closed = False
        for sel in ('[data-qa="modal-overlay"] button[data-qa*="close"]',
                    '[data-qa="modal-overlay"] button[aria-label*="акры"]',
                    '[data-qa="modal-overlay"] [data-qa="bloko-modal-close"]',
                    '[data-qa="modal-overlay"] button[data-qa*="reject"]'):
            el = await page.query_selector(sel)
            if el:
                try:
                    await el.click(timeout=2000); closed = True; break
                except Exception:
                    pass
        if not closed:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
        await page.wait_for_timeout(800)
    return (await page.query_selector('[data-qa="modal-overlay"]')) is None


async def modal_info(page):
    try:
        return await page.evaluate(r"""() => {
          const ov=document.querySelector('[data-qa=\"modal-overlay\"]');
          if(!ov) return null;
          const b=[...ov.querySelectorAll('button,a,[role=button]')].slice(0,12)
             .map(x=>({dq:x.getAttribute('data-qa'),al:x.getAttribute('aria-label'),t:(x.innerText||'').trim().slice(0,35)}));
          return {title:(ov.innerText||'').replace(/\s+/g,' ').slice(0,160), btns:b};
        }""")
    except Exception:
        return None


async def status_button(page):
    """Кнопка-ячейка «Статус поиска · …». Возвращает (handle, text)."""
    for b in await page.query_selector_all('button[data-qa="cell"]'):
        try:
            t = (await b.inner_text()) or ""
        except Exception:
            t = ""
        if "Статус поиска" in t:
            return b, " ".join(t.split())
    return None, ""


async def try_set(page, acc):
    await dismiss_overlays(page)
    btn, txt = await status_button(page)
    if not btn:
        return "no-control", txt
    if TARGET.lower() in txt.lower():
        return "already-active", txt
    try:
        await btn.click(timeout=5000)
    except Exception:
        await dismiss_overlays(page)
        try:
            await btn.click(timeout=5000, force=True)
        except Exception as e:
            mi = await modal_info(page)
            print(f"  [{acc}] open-fail modal={mi}")
            return f"open-fail:{e}", txt
    await page.wait_for_timeout(1300)
    opt = page.get_by_text(TARGET, exact=True)
    try:
        n = await opt.count()
    except Exception:
        n = 0
    if n == 0:
        return "option-not-found", txt
    try:
        await opt.first.click(timeout=5000)
    except Exception as e:
        return f"opt-click-fail:{e}", txt
    await page.wait_for_timeout(1800)
    _, txt2 = await status_button(page)
    ok = TARGET.lower() in (txt2 or "").lower()
    return ("set-ok" if ok else "set-unconfirmed"), (txt2 or txt)


DUMP_JS = r"""
() => {
  const KW = ['ищу работу','предложени','не ищу','статус поиск','рассматрив'];
  const out = [];
  for (const el of document.querySelectorAll('button,a,span,div,li,[data-qa],[role]')) {
    const t = (el.innerText||'').trim();
    if (!t || t.length > 70) continue;
    if (KW.some(s => t.toLowerCase().includes(s))) {
      out.push({tag: el.tagName, dq: el.getAttribute('data-qa'),
                role: el.getAttribute('role'), text: t});
    }
  }
  const seen=new Set(), u=[];
  for (const o of out){const k=o.tag+'|'+o.text; if(!seen.has(k)){seen.add(k);u.push(o);}}
  return u.slice(0,40);
}
"""


async def main():
    acc = pgconn.get_account()
    cfg = pgconn.app_config()
    user = pgconn.get_setting("auth.username")
    pw = pgconn.get_setting("auth.password")
    if not cfg.get("web_state") and not (user and pw):
        print(f"[{acc}] нет ни web_state, ни креды — пропуск"); return
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            storage_state=cfg.get("web_state") or None,
            viewport={"width": 1280, "height": 1000}, locale="ru-RU",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
        page = await ctx.new_page()
        await page.goto("https://hh.ru/applicant/resumes", timeout=40000, wait_until="domcontentloaded")
        await page.wait_for_timeout(1800)
        if any(x in page.url.lower() for x in ("login", "signup", "account", "auth")):
            if not (user and pw):
                print(f"[{acc}] сессия протухла, креды отсутствуют — пропуск (нужен ре-логин в кабинете)")
                await browser.close(); return
            print(f"[{acc}] сессия протухла -> логин по паролю")
            if not await web_login(page, user, pw):
                print(f"[{acc}] логин не удался (капча/OTP)"); await browser.close(); return
            pgconn.set_app_config("web_state", await ctx.storage_state())
            await page.goto("https://hh.ru/applicant/resumes", timeout=40000, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
        if DISCOVER:
            rows = await page.evaluate(DUMP_JS)
            for r in rows:
                print(f"  <{r['tag']}> dq={r['dq']} :: {r['text'][:62]}")
            await page.screenshot(path=f"/tmp/status_{acc}.png", full_page=True)
            await browser.close(); return
        res, txt = await try_set(page, acc)
        print(f"[{acc}] {res} :: {txt[:70]}")
        if res not in ("already-active", "set-ok"):
            try:
                pgconn.notify(pgconn.PRIORITY_MED, f"set_search_status[{acc}]: статус не выставлен ({res})", category="status", dedup_key=f"status-{acc}")
            except Exception:
                pass
        if res in ("set-ok", "set-unconfirmed"):
            pgconn.set_app_config("web_state", await ctx.storage_state())
        if res in ("no-control", "option-not-found", "set-unconfirmed"):
            try:
                await page.screenshot(path=f"/tmp/status_{acc}.png", full_page=True)
                print(f"  скрин -> /tmp/status_{acc}.png")
            except Exception:
                pass
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
