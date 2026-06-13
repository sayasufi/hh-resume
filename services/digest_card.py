"""Красивая PNG-карточка дайджеста: HTML+CSS -> Chromium (Playwright, уже в образе) -> PNG.
Без внешних сервисов и сетевых вызовов. render_png(data) -> bytes | None (None -> текстовый фолбэк).

Все каналы (hh / GetMatch / Habr / Telegram) показываются РАВНОЗНАЧНЫМИ блоками с барами
своих исходов; блок показывается всегда — даже если канал не подключён («не подключено»)
или без активности («нет откликов»).

data = {
  "who": str, "date": str, "status": str,
  "today": {"apps": int, "views": int, "invites": int},
  "resumes": [{"title": str, "sob": int, "total": int, "pct": int}, ...],
  "platforms": [
    {"emoji": str, "name": str, "unit": str, "status": "data"|"empty"|"off", "n": int,
     "bars": [{"emoji": str, "label": str, "n": int, "pct": int, "color": str}, ...],
     "note": str},
    ...
  ],
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
    res = d.get("resumes") or []
    plats = d.get("platforms") or []

    def frow(b):
        return (f'<div class="frow"><div class="fhead">'
                f'<span class="fl">{b.get("emoji", "")} {e(b.get("label", ""))}</span>'
                f'<span class="fnums"><b>{b.get("n", 0)}</b>'
                f'<span class="fp">{b.get("pct", 0)}%</span></span></div>'
                f'{_bar(b.get("pct", 0), b.get("color", "#60a5fa"))}</div>')

    def psection(p):
        if p.get("status") == "data":
            right, dim = f'сегодня +{p.get("today", 0)}', ""
            sub = f'<div class="psub">всего {p.get("n", 0)} {e(p.get("unit", "откликов"))}</div>'
        else:
            right, dim, sub = ("не подключено" if p.get("status") == "off" else "нет откликов"), " dim", ""
        head = (f'<div class="ptitle{dim}"><span>{p.get("emoji", "")} {e(p.get("name", ""))}</span>'
                f'<span class="pright">{e(right)}</span></div>')
        body = sub + "".join(frow(b) for b in p.get("bars", []))
        if p.get("note"):
            body += f'<div class="pnote">{e(p["note"])}</div>'
        return f'<div class="pcell">{head}{body}</div>'

    plat_html = "".join(psection(p) for p in plats)

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; padding:0; box-sizing:border-box; font-family:-apple-system,'Segoe UI',Roboto,'Noto Sans',sans-serif; }}
body {{ background:transparent; }}
.card {{ width:720px; background:#0e1320; color:#e8edf6; }}
.pgrid {{ display:grid; grid-template-columns:1fr 1fr; gap:1px;
         background:rgba(255,255,255,.07); border-top:1px solid rgba(255,255,255,.07); }}
.pcell {{ padding:20px 26px; background:#0e1320; min-height:208px; }}
.hdr {{ padding:26px 30px 22px; background:linear-gradient(135deg,#3b82f6 0%,#6366f1 55%,#8b5cf6 100%); }}
.who {{ font-size:27px; font-weight:800; letter-spacing:-.3px; }}
.date {{ font-size:15px; opacity:.9; margin-top:3px; font-weight:500; }}
.sect {{ padding:20px 30px; border-top:1px solid rgba(255,255,255,.06); }}
.stitle {{ font-size:12px; font-weight:700; letter-spacing:1.4px; text-transform:uppercase;
          color:#8b98ad; margin-bottom:14px; }}
.ptitle {{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom:15px;
          font-size:18px; font-weight:800; }}
.ptitle .pright {{ font-size:14px; font-weight:700; color:#9fb3cc; }}
.ptitle.dim {{ opacity:.45; }}
.psub {{ font-size:13px; color:#8b98ad; margin:-7px 0 13px; }}
.pnote {{ font-size:13.5px; color:#8b98ad; }}
.today {{ display:flex; gap:12px; }}
.tcell {{ flex:1; background:#161d2e; border-radius:16px; padding:16px 10px; text-align:center; }}
.tn {{ font-size:30px; font-weight:800; line-height:1; }}
.tl {{ font-size:12.5px; color:#97a3b6; margin-top:7px; }}
.frow {{ margin-bottom:14px; }}
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
  <div class="sect"><div class="stitle">Сегодня сделано</div>
    <div class="today">
      <div class="tcell"><div class="tn">{t.get("apps", 0)}</div><div class="tl">📨 откликов</div></div>
      <div class="tcell"><div class="tn">+{t.get("views", 0)}</div><div class="tl">👀 просмотров</div></div>
      <div class="tcell"><div class="tn">{t.get("reply", 0)}</div><div class="tl">💬 ответов</div></div>
    </div>
  </div>
  <div class="pgrid">{plat_html}</div>
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
