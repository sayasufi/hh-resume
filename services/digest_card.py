"""Красивая PNG-карточка дайджеста: HTML+CSS -> Chromium (Playwright, уже в образе) -> PNG.
Без внешних сервисов и сетевых вызовов. render_png(data) -> bytes | None (None -> текстовый фолбэк).

data = {
  "who": str, "date": str, "status": str,
  "today": {"apps": int, "views": int, "invites": int},
  "funnel": {"total": int, "sob": int, "resp": int, "disc": int,
             "sob_pct": int, "resp_pct": int, "disc_pct": int},
  "resumes": [{"title": str, "sob": int, "total": int, "pct": int}, ...],
}
"""
import html


def _bar(pct: int, color: str) -> str:
    pct = max(0, min(100, int(pct or 0)))
    return (f'<div class="bar"><div class="fill" style="width:{pct}%;'
            f'background:linear-gradient(90deg,{color},{color}cc)"></div></div>')


def build_html(d: dict) -> str:
    e = html.escape
    t = d.get("today") or {}
    f = d.get("funnel") or {}
    res = d.get("resumes") or []

    def frow(emoji, label, n, pct, color):
        return (f'<div class="frow"><div class="fhead">'
                f'<span class="fl">{emoji} {e(label)}</span>'
                f'<span class="fnums"><b>{n}</b><span class="fp">{pct}%</span></span>'
                f'</div>{_bar(pct, color)}</div>')

    funnel = (frow("🤝", "Собеседования", f.get("sob", 0), f.get("sob_pct", 0), "#34d399")
              + frow("💬", "Ответы", f.get("resp", 0), f.get("resp_pct", 0), "#60a5fa")
              + frow("❌", "Отказы", f.get("disc", 0), f.get("disc_pct", 0), "#f87171"))

    res_rows = "".join(
        f'<div class="rrow"><span class="rt">{e(r.get("title") or "—")}</span>'
        f'<span class="rn">🤝 {r.get("sob", 0)}/{r.get("total", 0)} · {r.get("pct", 0)}%</span></div>'
        for r in res[:3])
    res_block = (f'<div class="sect"><div class="stitle">По резюме</div>{res_rows}</div>'
                 if res_rows else "")

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; padding:0; box-sizing:border-box; font-family:-apple-system,'Segoe UI',Roboto,'Noto Sans',sans-serif; }}
body {{ background:transparent; }}
.card {{ width:600px; background:#0e1320; color:#e8edf6; border-radius:26px; overflow:hidden;
        box-shadow:0 20px 60px rgba(0,0,0,.45); }}
.hdr {{ padding:26px 30px 22px; background:linear-gradient(135deg,#3b82f6 0%,#6366f1 55%,#8b5cf6 100%); }}
.who {{ font-size:27px; font-weight:800; letter-spacing:-.3px; }}
.date {{ font-size:15px; opacity:.9; margin-top:3px; font-weight:500; }}
.sect {{ padding:20px 30px; border-top:1px solid rgba(255,255,255,.06); }}
.stitle {{ font-size:12px; font-weight:700; letter-spacing:1.4px; text-transform:uppercase;
          color:#8b98ad; margin-bottom:14px; }}
.today {{ display:flex; gap:12px; }}
.tcell {{ flex:1; background:#161d2e; border-radius:16px; padding:16px 10px; text-align:center; }}
.tn {{ font-size:30px; font-weight:800; line-height:1; }}
.tl {{ font-size:12.5px; color:#97a3b6; margin-top:7px; }}
.frow {{ margin-bottom:15px; }}
.frow:last-child {{ margin-bottom:0; }}
.fhead {{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom:7px; }}
.fl {{ font-size:16px; font-weight:600; }}
.fnums b {{ font-size:18px; font-weight:800; }}
.fp {{ font-size:13px; color:#8b98ad; margin-left:9px; }}
.bar {{ height:9px; background:#1b2336; border-radius:6px; overflow:hidden; }}
.fill {{ height:100%; border-radius:6px; }}
.rrow {{ display:flex; justify-content:space-between; align-items:center; padding:9px 0;
        border-bottom:1px solid rgba(255,255,255,.05); font-size:15px; }}
.rrow:last-child {{ border-bottom:none; }}
.rt {{ font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:340px; }}
.rn {{ color:#9fb3cc; font-weight:600; font-size:14px; white-space:nowrap; }}
.status {{ padding:16px 30px 22px; font-size:15px; font-weight:600; color:#cdd6e4; }}
</style></head><body><div class="card" id="card">
  <div class="hdr"><div class="who">✨ {e(d.get("who") or "Кандидат")}</div>
    <div class="date">Сводка · {e(d.get("date") or "")}</div></div>
  <div class="sect"><div class="stitle">Коротко</div>
    <div class="today">
      <div class="tcell"><div class="tn">{t.get("apps", 0)}</div><div class="tl">📨 откликов сегодня</div></div>
      <div class="tcell"><div class="tn">+{t.get("views", 0)}</div><div class="tl">👀 новых просмотров</div></div>
      <div class="tcell"><div class="tn">+{t.get("invites", 0)}</div><div class="tl">💬 непрочитанных</div></div>
    </div>
  </div>
  <div class="sect"><div class="stitle">Воронка · {f.get("total", 0)} откликов</div>{funnel}</div>
  {res_block}
  <div class="status">{e(d.get("status") or "✅ Бот работает штатно")}</div>
</div></body></html>"""


async def render_png(data: dict) -> bytes | None:
    """HTML-карточку -> PNG (bytes). None при любом сбое рендера -> текстовый фолбэк."""
    try:
        from playwright.async_api import async_playwright
        markup = build_html(data)
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
            try:
                page = await browser.new_page(
                    viewport={"width": 640, "height": 200}, device_scale_factor=2)
                await page.set_content(markup, wait_until="load")
                el = await page.query_selector("#card")
                png = await (el.screenshot() if el else page.screenshot())
                return png
            finally:
                await browser.close()
    except Exception as ex:  # noqa: BLE001
        print("digest_card: рендер не удался:", repr(ex)[:140])
        return None
