"use strict";
const tg = window.Telegram ? window.Telegram.WebApp : null;
const INIT = tg ? tg.initData : "";
if (tg) { tg.ready(); tg.expand(); try { tg.setHeaderColor("secondary_bg_color"); } catch (e) {} }

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const err = (m) => { const e = $("#err"); e.textContent = m; e.classList.remove("hidden"); setTimeout(() => e.classList.add("hidden"), 4000); };
const hap = (k) => { try { if (!tg || !tg.HapticFeedback) return; k === "sel" ? tg.HapticFeedback.selectionChanged() : tg.HapticFeedback.impactOccurred("light"); } catch (e) {} };
// состояние ошибки загрузки (отличаем «пусто» от «не загрузилось») + повтор по тапу
function failBox(boxSel, countSel, retry) {
  const box = $(boxSel); if (!box) return;
  if (countSel && $(countSel)) $(countSel).textContent = "!";
  box.innerHTML = '<div class="empty err-state">Не удалось загрузить · нажмите, чтобы повторить</div>';
  const e = box.querySelector(".err-state"); if (e) e.onclick = retry;
}

let VIEW_ACCOUNT = null;  // админ: смотрим выбранный аккаунт (account-override)
async function api(path, opts = {}) {
  if (VIEW_ACCOUNT) path += (path.includes("?") ? "&" : "?") + "account=" + encodeURIComponent(VIEW_ACCOUNT);
  const r = await fetch(path, { ...opts, headers: { "X-Init-Data": INIT, "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (r.status === 404) throw new Error("not_linked");
  if (!r.ok) {
    let detail = "HTTP " + r.status;
    try { const j = await r.json(); if (j && j.detail) detail = j.detail; } catch (e) {}
    throw new Error(detail);
  }
  return r.json();
}
const save = (key, value) => api("/api/settings", { method: "POST", body: JSON.stringify({ key, value }) });

// вкладки
document.querySelectorAll("#tabs button").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active"); $("#tab-" + b.dataset.tab).classList.add("active");
    if (b.dataset.tab === "actions") loadActions();
    window.scrollTo(0, 0); hap("sel");
  };
});

function renderMe(d) {
  const p = (d && d.profile) || {}, s = (d && d.stats) || {};
  $("#avatar").textContent = (p.name || "·").trim().charAt(0).toUpperCase() || "·";
  $("#hname").textContent = p.name || "—";
  const stt = p.status || "";
  const st = $("#hstatus"); st.textContent = stt;
  const sk = p.status_kind || (stt.includes("работает") ? "ok" : stt.indexOf("всё") === 0 ? "off" : "paused");
  st.className = "pill " + (sk === "ok" ? "good" : sk === "off" ? "bad" : "warn");
  $("#p-name").textContent = p.name || "—";
  const bd = s.breakdown || [];
  $("#breakdown").innerHTML = bd.length ? bd.map((b) =>
    `<div class="stat"><div class="num">${b.value}</div><div class="lbl">${b.emoji} ${esc(b.label)}</div></div>`).join("")
    : '<div class="empty">Нет данных за период</div>';
  $("#next-apply").textContent = d.next_apply
    ? "⏱ Следующие обычные отклики: " + d.next_apply
    : "⏸ Обычные отклики на паузе — включи «Авто-отклики» в Функциях.";
}

// здоровье источников на вкладке «Профиль» (из /api/settings → sources)
function renderSources(sources) {
  const box = $("#sources");
  if (!box) return;
  const cls = { ok: "ok", warn: "wait", down: "bad", off: "wait" };
  const ico = { ok: "✅", warn: "⚠️", down: "🔴", off: "⏸" };
  box.innerHTML = (sources || []).map((s) => {
    const sub = [s.detail, s.run].filter(Boolean).join(" · ");
    return '<div class="cell"><div class="dlg-main">'
      + `<div class="dlg-title">${esc(s.src)} `
      + `<span class="gm-st ${cls[s.state] || "wait"}">${ico[s.state] || ""} ${esc(s.label)}</span></div>`
      + (sub ? `<div class="dlg-date">${esc(sub)}</div>` : "")
      + "</div></div>";
  }).join("");
}

function renderTrend(days) {
  const box = $("#trend");
  if (!box) return;  // график динамики убран из Статы
  if (!days || days.length < 2) { box.innerHTML = '<div class="empty">График появится за пару дней использования</div>'; return; }
  const vals = days.map((d) => d.applications), max = Math.max(1, ...vals);
  const W = 320, H = 88, n = days.length;
  const pts = vals.map((v, i) => [i * (W / (n - 1)), H - (v / max) * (H - 12) - 6]);
  const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  const area = `M0 ${H} ` + pts.map((p) => "L" + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ") + ` L${W} ${H} Z`;
  const dots = pts.map((p) => `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="2.5"/>`).join("");
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="chart" preserveAspectRatio="none">`
    + `<path class="area" d="${area}"/><path class="ln" d="${line}" fill="none"/>${dots}</svg>`
    + `<div class="chart-x"><span>${esc(days[0].day.slice(5))}</span><span>${esc(days[n - 1].day.slice(5))}</span></div>`;
}

// ── отклики: фильтр + сортировка + клик ──
let DIALOGS = [], FILTER = "all", SORT = "date";
function renderDialogs() {
  const box = $("#dialogs");
  let arr = DIALOGS.filter((d) =>
    FILTER === "all" ? true :
    FILTER === "sob" ? ["interview", "invitation", "hired"].includes(d.state_id) :
    FILTER === "discard" ? (d.state_id || "").startsWith("discard") :
    d.state_id === FILTER);
  $("#dlg-count").textContent = arr.length;
  if (SORT === "status") arr = [...arr].sort((a, b) => a.rank - b.rank);
  if (!arr.length) { box.innerHTML = '<div class="empty">Ничего не найдено</div>'; return; }
  box.innerHTML = '<div class="list">' + arr.map((d) =>
    `<div class="cell dlg tap" data-id="${esc(d.id)}"><div class="dlg-main">`
    + `<div class="dlg-title">${esc(d.title)}</div>`
    + `<div class="dlg-emp">${esc(d.employer)}</div>`
    + `<div class="dlg-st">${d.emoji} ${esc(d.state)}${d.has_updates ? ' <span class="dot"></span>' : ""}</div></div>`
    + `<div class="dlg-side"><span class="dlg-date">${esc(d.updated)}</span><span class="chev">›</span></div></div>`).join("") + "</div>";
  box.querySelectorAll(".dlg").forEach((el) => { el.onclick = () => openDialog(el.dataset.id); });
}
$("#dlg-filter").querySelectorAll(".chip").forEach((c) => {
  c.onclick = () => {
    $("#dlg-filter").querySelectorAll(".chip").forEach((x) => x.classList.remove("active"));
    c.classList.add("active"); FILTER = c.dataset.f; renderDialogs(); hap("sel");
  };
});
$("#dlg-sort").querySelectorAll("button").forEach((b) => {
  b.onclick = () => {
    $("#dlg-sort").querySelectorAll("button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active"); SORT = b.dataset.s; renderDialogs(); hap("sel");
  };
});

const _anySheet = () => document.querySelector(".sheet-wrap:not(.hidden)");
function openSheet(id) { $(id).classList.remove("hidden"); try { if (tg && tg.BackButton) tg.BackButton.show(); } catch (e) {} }
function closeSheet(id) { $(id).classList.add("hidden"); try { if (tg && tg.BackButton && !_anySheet()) tg.BackButton.hide(); } catch (e) {} }
const closeAllSheets = () => document.querySelectorAll(".sheet-wrap:not(.hidden)").forEach((w) => closeSheet("#" + w.id));
document.querySelectorAll(".sheet-wrap").forEach((w) => { w.onclick = (e) => { if (e.target === w) closeSheet("#" + w.id); }; });
document.querySelectorAll("[data-close]").forEach((el) => { el.onclick = (e) => { e.stopPropagation(); closeSheet("#" + el.closest(".sheet-wrap").id); }; });
try { if (tg && tg.BackButton) tg.BackButton.onClick(closeAllSheets); } catch (e) {}

async function openDialog(id) {
  const d = DIALOGS.find((x) => String(x.id) === String(id));
  if (!d) return; hap("sel");
  $("#m-title").textContent = d.title;
  $("#m-emp").textContent = d.employer + " · " + d.state;
  const hh = $("#m-hh");
  if (d.url) {
    hh.classList.remove("hidden");
    hh.onclick = (e) => { e.preventDefault(); hap("sel"); if (tg && tg.openLink) tg.openLink(d.url); else window.open(d.url, "_blank"); };
  } else hh.classList.add("hidden");
  $("#m-body").innerHTML = '<div class="empty">Загрузка…</div>';
  openSheet("#modal");
  if (d.has_updates) { d.has_updates = false; renderDialogs(); }  // сбрасываем синюю точку
  try {
    const r = await api("/api/dialog?id=" + encodeURIComponent(id));
    if (!r.messages || !r.messages.length) { $("#m-body").innerHTML = '<div class="empty">Сообщений нет</div>'; return; }
    $("#m-body").innerHTML = r.messages.map((m) =>
      `<div class="msg ${m.me ? "me" : "them"}"><div class="bub">${esc(m.text)}</div><div class="mt">${esc(m.at)}</div></div>`).join("");
    $("#m-body").scrollTop = $("#m-body").scrollHeight;
  } catch (e) { $("#m-body").innerHTML = '<div class="empty">Не удалось загрузить переписку</div>'; }
}

// ── функции / настройки ──
let RESUMES = [], RESUME_ID = "";
// подсказка по привязке GetMatch (сама привязка — в боте: /addaccount → GetMatch)
function renderGmLink(st) {
  const box = $("#gm-link");
  if (!box) return;
  if (st.getmatch_linked || st.tg_connected) { box.style.display = "none"; return; }
  box.style.display = "";
  box.textContent = "Чтобы подключить GetMatch — открой бота и набери /addaccount → GetMatch "
    + "(код придёт в @g_jobbot).";
}
function wireGmLink() {}  // привязка перенесена в бот, инлайн-форма убрана
function bindToggles(features, tgConnected, gmLinked, habrLinked, hhConnected) {
  document.querySelectorAll(".toggle input[data-feat]").forEach((inp) => {
    inp.checked = !!features[inp.dataset.feat];
    // giga нужен Telegram; getmatch — Telegram ИЛИ логин+код; habr — вход на career.habr.com; hh-функции — привязка hh
    const lockGiga = inp.dataset.feat === "giga" && !tgConnected;
    const lockGm = inp.dataset.feat === "getmatch" && !tgConnected && !gmLinked;
    const lockHabr = (inp.dataset.feat === "habr" || inp.dataset.feat === "habr_chat") && !habrLinked;
    const lockTg = inp.dataset.feat === "tg_channels" && !tgConnected;
    const lockHh = ["apply", "tests", "reply", "browse"].includes(inp.dataset.feat) && !hhConnected;
    const lock = lockGiga || lockGm || lockHabr || lockTg || lockHh;
    inp.disabled = lock;
    if (lock) inp.checked = false;
    inp.closest(".toggle").classList.toggle("disabled", lock);
    inp.onchange = async () => {
      const row = inp.closest(".toggle"); row.classList.add("busy");
      try { await save(inp.dataset.feat, inp.checked); hap("light"); }
      catch (e) { inp.checked = !inp.checked; err((e && e.message) || "Не удалось сохранить"); }
      finally { row.classList.remove("busy"); }
    };
  });
}

function resumeTitle(id) { const r = RESUMES.find((x) => String(x.id) === String(id)); return r ? (r.title || r.id) : (id || "—"); }

// категории TG-каналов: тумблеры ниш (вместо текстового поля)
function _tgChanChips(arr, rm) {  // чипы каналов (кликабельны -> t.me); rm=true -> с ✕ для удаления
  return arr.map((u) => `<button class="chip" data-${rm ? "rm" : "u"}="${esc(u)}">@${esc(u)}${rm ? " ✕" : ""}</button>`).join("");
}
function _wireChanOpen(box) {
  box.querySelectorAll(".chip[data-u]").forEach((b) => {
    b.onclick = () => { const l = "https://t.me/" + b.dataset.u; if (tg && tg.openLink) tg.openLink(l); else window.open(l, "_blank"); };
  });
}
function renderTgCats(catalog, catsStr, customStr, enabled) {
  const box = $("#tg-cats"), title = $("#tg-cats-title");
  if (!box) return;
  catalog = catalog || [];
  box.classList.toggle("off", !enabled);   // нет Telegram → категории неактивны
  if (title) title.style.display = catalog.length ? "" : "none";
  if (!catalog.length) { box.innerHTML = ""; renderTgCustom(customStr, enabled); return; }
  const sel = new Set((catsStr || "").split(",").map((s) => s.trim()).filter(Boolean));
  box.innerHTML = catalog.map((c) =>
    `<div class="cell toggle cat-row">`
    + `<span class="t cat-name" data-exp="${esc(c.key)}"><b>${esc(c.label)} <em class="dim">${c.channels.length}</em></b>`
    + `<small>нажми, чтобы посмотреть каналы ⌄</small></span>`
    + `<input type="checkbox" data-cat="${esc(c.key)}"${sel.has(c.key) ? " checked" : ""}><i data-sw="${esc(c.key)}"></i></div>`
    + `<div class="cat-chans chips" data-chans="${esc(c.key)}" style="display:none">${_tgChanChips(c.channels)}</div>`
  ).join("");
  box.querySelectorAll(".cat-name[data-exp]").forEach((el) => {
    el.onclick = () => { const d = box.querySelector(`.cat-chans[data-chans="${el.dataset.exp}"]`); if (d) d.style.display = d.style.display === "none" ? "" : "none"; hap("sel"); };
  });
  box.querySelectorAll("i[data-sw]").forEach((sw) => {
    sw.onclick = async () => {
      const inp = box.querySelector(`input[data-cat="${sw.dataset.sw}"]`);
      inp.checked = !inp.checked;
      if (inp.checked) sel.add(sw.dataset.sw); else sel.delete(sw.dataset.sw);
      try { await save("tg.cats", [...sel].join(",")); hap("light"); }
      catch (e) { inp.checked = !inp.checked; err("Не удалось сохранить"); }
    };
  });
  _wireChanOpen(box);
  renderTgCustom(customStr, enabled);
}
function renderTgCustom(customStr, enabled) {
  const box = $("#tg-custom"), title = $("#tg-custom-title");
  if (!box) return;
  if (title) title.style.display = "";
  box.classList.toggle("off", !enabled);   // нет Telegram → поле «добавить канал» неактивно
  let list = (customStr || "").split(",").map((s) => s.trim().replace(/^@/, "")).filter(Boolean);
  const draw = () => {
    box.innerHTML = '<div class="cell"><input type="text" id="tg-custom-input" placeholder="добавить @канал" autocapitalize="off" autocomplete="off" spellcheck="false" style="flex:1;background:transparent;border:none;color:var(--accent);font:inherit;outline:none">'
      + '<button class="chip" id="tg-custom-add">Добавить</button></div>'
      + (list.length ? '<div class="cat-chans chips">' + _tgChanChips(list, true) + '</div>' : "");
    $("#tg-custom-add").onclick = async () => {
      const v = (($("#tg-custom-input").value || "").trim().replace(/^@/, "").replace(/[^a-zA-Z0-9_]/g, ""));
      if (!v || list.includes(v)) return;
      list.push(v);
      try { await save("tg.channels", list.join(",")); hap("light"); draw(); }
      catch (e) { list.pop(); err("Не удалось сохранить"); }
    };
    $("#tg-custom-input").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); $("#tg-custom-add").click(); } };
    box.querySelectorAll(".chip[data-rm]").forEach((b) => {
      b.onclick = async () => {
        const prev = list.slice(); list = list.filter((x) => x !== b.dataset.rm);
        try { await save("tg.channels", list.join(",")); hap("sel"); draw(); }
        catch (e) { list = prev; err("Не удалось сохранить"); }
      };
    });
  };
  draw();
}
function bindConfig(cfg, resumes, hhConnected, tgConnected) {
  RESUMES = resumes || []; RESUME_ID = cfg.resume_id || (RESUMES[0] && RESUMES[0].id) || "";
  const capL = cfg.max_per_day_cap || 200;
  $("#cfg-salary").value = cfg.salary || "";
  $("#cfg-limit").value = cfg.max_per_day != null ? cfg.max_per_day : "";
  $("#cfg-limit").max = capL;
  if ($("#cap-limit")) $("#cap-limit").textContent = "(макс " + capL + ")";
  const updateTNote = () => {  // тесты = 25% от лимита откликов
    const n = Math.round((parseInt($("#cfg-limit").value || "0", 10) || 0) * 0.25);
    const el = $("#tlimit-note");
    if (el) el.textContent = n ? `+${n}/день (25% от лимита)` : "+25% к лимиту";
  };
  updateTNote();
  $("#cfg-limit").addEventListener("input", updateTNote);
  $("#resume-val").textContent = resumeTitle(RESUME_ID);
  const wire = (el, key) => {
    el.onchange = async () => {
      el.classList.add("busy");
      try { await save(key, el.value); hap("light"); }
      catch (e) { err("Не удалось сохранить"); } finally { el.classList.remove("busy"); }
    };
  };
  const clampWire = (el, key, max) => {
    el.onchange = async () => {
      const n = Math.min(max, Math.max(0, parseInt(el.value || "0", 10) || 0));
      el.value = n; el.classList.add("busy");
      try { await save(key, n); hap("light"); }
      catch (e) { err("Не удалось сохранить"); } finally { el.classList.remove("busy"); }
    };
  };
  wire($("#cfg-salary"), "salary");
  clampWire($("#cfg-limit"), "apply.max_per_day", capL);
  if ($("#cfg-gm-limit")) {
    const capG = cfg.getmatch_max_per_day_cap || 50;
    $("#cfg-gm-limit").value = cfg.getmatch_max_per_day != null ? cfg.getmatch_max_per_day : "";
    $("#cfg-gm-limit").max = capG;
    if ($("#cap-glimit")) $("#cap-glimit").textContent = "(макс " + capG + ")";
    clampWire($("#cfg-gm-limit"), "getmatch.max_per_day", capG);
  }
  if ($("#cfg-habr-limit")) {
    const capH = cfg.habr_max_per_day_cap || 30;
    $("#cfg-habr-limit").value = cfg.habr_max_per_day != null ? cfg.habr_max_per_day : "";
    $("#cfg-habr-limit").max = capH;
    if ($("#cap-hlimit")) $("#cap-hlimit").textContent = "(макс " + capH + ")";
    clampWire($("#cfg-habr-limit"), "habr.max_per_day", capH);
  }
  renderTgCats(cfg.tg_catalog, cfg.tg_cats, cfg.tg_channels, !!tgConnected);
  // hh не привязан → профиль откликов (зарплата, резюме, лимит, ГПХ) неактивен
  const hhOff = !hhConnected;
  ["#cfg-salary", "#cfg-limit"].forEach((id) => {
    const el = $(id); if (el) { el.disabled = hhOff; const c = el.closest(".cell"); if (c) c.classList.toggle("off", hhOff); }
  });
  if ($("#resume-row")) { $("#resume-row").disabled = hhOff; $("#resume-row").classList.toggle("off", hhOff); }
  if ($("#cfg-gph")) { $("#cfg-gph").disabled = hhOff; const c = $("#cfg-gph").closest(".cell"); if (c) c.classList.toggle("off", hhOff); }
  const gph = $("#cfg-gph");
  if (gph) {
    gph.checked = !!cfg.civil_law_only;
    gph.onchange = async () => {
      const row = gph.closest(".toggle"); row.classList.add("busy");
      try { await save("apply.civil_law_only", gph.checked); hap("light"); }
      catch (e) { gph.checked = !gph.checked; err("Не удалось сохранить"); }
      finally { row.classList.remove("busy"); }
    };
  }
}
$("#resume-row").onclick = () => {
  if (!RESUMES.length) return;
  $("#pk-title").textContent = "Активное резюме";
  $("#pk-body").innerHTML = '<div class="list">' + RESUMES.map((r) =>
    `<div class="cell tap pk-opt${String(r.id) === String(RESUME_ID) ? " sel" : ""}" data-id="${esc(r.id)}">`
    + `<span>${esc(r.title || r.id)}</span>${String(r.id) === String(RESUME_ID) ? '<span class="ok">✓</span>' : ""}</div>`).join("") + "</div>";
  openSheet("#picker"); hap("sel");
  $("#pk-body").querySelectorAll(".pk-opt").forEach((el) => {
    el.onclick = async () => {
      RESUME_ID = el.dataset.id; $("#resume-val").textContent = resumeTitle(RESUME_ID);
      closeSheet("#picker"); hap("light");
      try { await save("apply.resume_id", RESUME_ID); } catch (e) { err("Не удалось сохранить"); }
    };
  });
};

// активность бота (счётчики реальных действий)
function renderActivity(a) {
  $("#a-apply").textContent = a.apply || 0;
  $("#a-tests").textContent = a.tests || 0;
  $("#a-reply").textContent = a.reply || 0;
  $("#a-browse").textContent = a.browse || 0;
  $("#a-bump").textContent = a.bump || 0;
}
const loadActivity = () => api("/api/activity" + qp()).then(renderActivity).catch(() => {});

// прогресс авто-ГигаРекрутера (giga_queue) — раньше был полностью невидим
function renderGiga(g) {
  if ($("#a-giga")) $("#a-giga").textContent = (g && g.done) || 0;  // блок «Авто-задачи в Telegram» в Стате
  const box = $("#giga-card");
  if (!box) return;  // карточка убрана с профиля — прогресс теперь в Стате
  if (!g || (!g.pending && !g.done && !g.active)) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const last = g.last && g.last.vacancy
    ? `<div class="giga-last">Последнее: ${esc(g.last.vacancy)} · ${esc(g.last.at)}</div>` : "";
  box.innerHTML = '<div class="giga-h">🤖 Бот сам проходит анкеты и интервью в Telegram</div>'
    + '<div class="giga-row">'
    + `<span class="gnum"><b>${g.done | 0}</b> пройдено</span>`
    + `<span class="gnum"><b>${g.pending | 0}</b> в очереди</span>`
    + (g.active ? `<span class="gnum"><b>${g.active | 0}</b> сейчас</span>` : "")
    + '</div>' + last;
}
const loadGiga = () => api("/api/giga").then(renderGiga).catch(() => {});

// отклики GetMatch со статусами: список во вкладке «Отклики» + разбивка в «Стате»
let GM_APPS = [], GM_FILTER = "all";
const gmCls = (a) => {
  const m = (a.status || "").toLowerCase();  // стабильный машинный код — приоритетно
  if (/(approv|accept|invit|offer|hir)/.test(m)) return "ok";
  if (/(reject|declin|refus)/.test(m)) return "bad";
  const s = (a.status_readable || "").toLowerCase();  // запасной матч по тексту
  if (s.includes("одобр") || s.includes("приглаш") || s.includes("оффер")) return "ok";
  if (s.includes("отказ")) return "bad";
  return "wait";
};
function renderGmApps() {
  const box = $("#gm-apps"), cnt = $("#gm-count");
  if (!box) return;
  const items = GM_FILTER === "all" ? GM_APPS : GM_APPS.filter((a) => gmCls(a) === GM_FILTER);
  if (cnt) cnt.textContent = items.length;
  if (!items.length) {
    box.innerHTML = '<div class="empty">' +
      (GM_APPS.length ? "Нет откликов в этом фильтре" : "Пока нет откликов через GetMatch") + "</div>";
    return;
  }
  box.innerHTML = '<div class="list">' + items.map((a) => {
    const sub = [a.company, a.at].filter(Boolean).join(" · ");
    const st = a.status_readable ? `<span class="gm-st ${gmCls(a)}">${esc(a.status_readable)}</span>` : "";
    const rej = a.reject_reason ? ` · ${esc(a.reject_reason)}` : "";
    return '<div class="cell act"><div class="dlg-main">'
      + `<div class="dlg-title">${esc(a.title)} ${st}</div>`
      + `<div class="dlg-date">${esc(sub)}${rej}</div></div>`
      + (a.url ? `<button class="abtn open" data-url="${esc(a.url)}">↗</button>` : "") + "</div>";
  }).join("") + "</div>";
  box.querySelectorAll(".abtn[data-url]").forEach((el) => {
    el.onclick = () => { hap("sel"); if (tg && tg.openLink) tg.openLink(el.dataset.url); else window.open(el.dataset.url, "_blank"); };
  });
}
function _statusRows(box, empty, apps) {
  if (!box) return;
  if (empty) empty.style.display = apps.length ? "none" : "";
  if (!apps.length) { box.innerHTML = ""; return; }
  const c = { wait: 0, ok: 0, bad: 0 };
  apps.forEach((a) => { c[gmCls(a)]++; });
  const card = (n, lbl) => `<div class="stat"><div class="num">${n}</div><div class="lbl">${lbl}</div></div>`;
  box.innerHTML = card(apps.length, "Откликов отправлено") + card(c.wait, "Ждём ответа")
    + card(c.ok, "Одобрены / приглашения") + card(c.bad, "Отказы");
}
function renderGmStats() { _statusRows($("#gm-stats"), $("#gm-empty"), GM_APPS); }
const loadGetmatchApps = () => api("/api/getmatch").then((r) => {
  GM_APPS = r.applications || []; renderGmApps(); renderGmStats();
}).catch(() => {});
// под-вкладки в «Настройках» (hh / GetMatch / Habr / Telegram / Общие)
if ($("#set-nav")) $("#set-nav").querySelectorAll("button").forEach((b) => {
  b.onclick = () => {
    $("#set-nav").querySelectorAll("button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    document.querySelectorAll("#tab-feat .set-panel").forEach((p) => {
      p.style.display = p.dataset.panel === b.dataset.set ? "" : "none";
    });
    hap("sel");
  };
});
// переключатель источника в «Откликах» (hh / GetMatch)
if ($("#dlg-src")) $("#dlg-src").querySelectorAll("button").forEach((b) => {
  b.onclick = () => {
    $("#dlg-src").querySelectorAll("button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    const src = b.dataset.src;
    $("#src-hh").style.display = src === "hh" ? "" : "none";
    $("#src-getmatch").style.display = src === "getmatch" ? "" : "none";
    $("#src-habr").style.display = src === "habr" ? "" : "none";
    if (src === "getmatch") loadGetmatchApps();  // свежие отклики (надёжно, не по тайму boot)
    if (src === "habr") loadHabrApps();
    hap("sel");
  };
});
// фильтр по статусу в GetMatch
if ($("#gm-filter")) $("#gm-filter").querySelectorAll(".chip").forEach((c) => {
  c.onclick = () => {
    $("#gm-filter").querySelectorAll(".chip").forEach((x) => x.classList.remove("active"));
    c.classList.add("active"); GM_FILTER = c.dataset.gf; renderGmApps(); hap("sel");
  };
});

// ── отклики Habr (зеркало GetMatch) ──
let HABR_APPS = [], HABR_FILTER = "all";
function renderHabrApps() {
  const box = $("#habr-apps"), cnt = $("#habr-count");
  if (!box) return;
  const items = HABR_FILTER === "all" ? HABR_APPS : HABR_APPS.filter((a) => gmCls(a) === HABR_FILTER);
  if (cnt) cnt.textContent = items.length;
  if (!items.length) {
    box.innerHTML = '<div class="empty">'
      + (HABR_APPS.length ? "Нет откликов в этом фильтре" : "Пока нет откликов через Habr") + "</div>";
    return;
  }
  box.innerHTML = '<div class="list">' + items.map((a) => {
    const sub = [a.company, a.at].filter(Boolean).join(" · ");
    const st = a.status_readable ? `<span class="gm-st ${gmCls(a)}">${esc(a.status_readable)}</span>` : "";
    return '<div class="cell act"><div class="dlg-main">'
      + `<div class="dlg-title">${esc(a.title)} ${st}</div>`
      + `<div class="dlg-date">${esc(sub)}</div></div>`
      + (a.url ? `<button class="abtn open" data-url="${esc(a.url)}">↗</button>` : "") + "</div>";
  }).join("") + "</div>";
  box.querySelectorAll(".abtn[data-url]").forEach((el) => {
    el.onclick = () => { hap("sel"); if (tg && tg.openLink) tg.openLink(el.dataset.url); else window.open(el.dataset.url, "_blank"); };
  });
}
function renderHabrStats() { _statusRows($("#habr-stats"), $("#habr-empty"), HABR_APPS); }
const loadHabrApps = () => api("/api/habr").then((r) => {
  HABR_APPS = r.applications || []; renderHabrApps(); renderHabrStats();
}).catch(() => {});
if ($("#habr-filter")) $("#habr-filter").querySelectorAll(".chip").forEach((c) => {
  c.onclick = () => {
    $("#habr-filter").querySelectorAll(".chip").forEach((x) => x.classList.remove("active"));
    c.classList.add("active"); HABR_FILTER = c.dataset.hf; renderHabrApps(); hap("sel");
  };
});

// дела (что нужно сделать самому)
function renderActions(items) {
  const box = $("#actions");
  $("#act-count").textContent = items.length;
  if (!items.length) { box.innerHTML = '<div class="empty">Дел нет — всё под контролем 👌</div>'; return; }
  box.innerHTML = '<div class="list">' + items.map((a) =>
    `<div class="cell act"><div class="dlg-main act-text">`
    + `<div class="dlg-title">${esc(a.action)}</div>`
    + `<div class="dlg-emp">${esc(a.vacancy)}</div>`
    + `<div class="dlg-date">${esc(a.created_at)} · нажми, чтобы раскрыть</div></div>`
    + `<div class="act-btns">`
    + (a.chat_url ? `<button class="abtn chat" data-url="${esc(a.chat_url)}">Чат</button>` : "")
    + `<button class="abtn del" data-id="${a.id}" title="Удалить — вакансия не интересна">🗑</button>`
    + `<button class="abtn done" data-id="${a.id}" title="Выполнено">✓</button></div></div>`).join("") + "</div>";
  box.querySelectorAll(".abtn[data-url]").forEach((el) => {
    el.onclick = () => { hap("sel"); if (tg && tg.openLink) tg.openLink(el.dataset.url); else window.open(el.dataset.url, "_blank"); };
  });
  // тап по тексту дела — раскрыть/свернуть полный текст (часто обрезано)
  box.querySelectorAll(".act-text").forEach((el) => {
    el.onclick = () => { el.closest(".act").classList.toggle("expanded"); hap("sel"); };
  });
  const actBtn = (cls, path) => box.querySelectorAll(cls).forEach((el) => {
    el.onclick = async () => {
      const row = el.closest(".act"); row.style.opacity = ".4";
      try { await api(path, { method: "POST", body: JSON.stringify({ id: parseInt(el.dataset.id, 10) }) }); hap("light"); loadActions(); }
      catch (e) { err("Не удалось"); row.style.opacity = "1"; }
    };
  });
  actBtn(".abtn.done", "/api/action_done");
  actBtn(".abtn.del", "/api/action_delete");
}
const loadActions = () => api("/api/actions").then((r) => renderActions(r.items || []))
  .catch(() => failBox("#actions", "#act-count", loadActions));

// период — диапазон дат {dfrom, dto}; пресеты + произвольные даты
const _iso = (off) => { const d = new Date(); d.setDate(d.getDate() - off); return d.toISOString().slice(0, 10); };
function _preset(key) {
  const t = _iso(0);
  if (key === "today") return { dfrom: t, dto: t };
  if (key === "yesterday") { const y = _iso(1); return { dfrom: y, dto: y }; }
  if (key === "week") return { dfrom: _iso(6), dto: t };
  if (key === "month") return { dfrom: _iso(29), dto: t };
  return { dfrom: "", dto: "" };  // all
}
let PERIOD = _preset("week");
const qp = () => {
  const s = [];
  if (PERIOD.dfrom) s.push("dfrom=" + PERIOD.dfrom);
  if (PERIOD.dto) s.push("dto=" + PERIOD.dto);
  return s.length ? "?" + s.join("&") : "";
};
const loadStats = () => api("/api/me" + qp()).then(renderMe).catch(() => {});
function showFresh(age) {
  const el = $("#dlg-fresh"); if (!el) return;
  if (age == null) { el.textContent = ""; return; }
  const m = Math.round(age / 60);
  el.textContent = m <= 0 ? "обновлено только что" : "обновлено " + m + " мин назад";
}
const loadDialogs = () => api("/api/dialogs" + qp())
  .then((r) => { DIALOGS = r.items || []; renderDialogs(); showFresh(r.synced_age); })
  .catch(() => failBox("#dialogs", "#dlg-count", loadDialogs));
// ручное обновление (кнопка в шапке) — перетягивает всё актуальное
function refreshAll() {
  hap("light");
  loadStats(); loadActivity(); loadDialogs(); loadActions(); loadGiga(); loadGetmatchApps(); loadHabrApps();
  api("/api/trends").then((t) => renderTrend(t.days)).catch(() => {});
}
if ($("#refresh")) $("#refresh").onclick = refreshAll;
// период влияет на воронку, детали, активность бота и список откликов
const _reloadPeriod = () => { loadStats(); loadActivity(); loadDialogs(); };
document.querySelectorAll(".period button").forEach((b) => {
  b.onclick = () => {
    const key = b.dataset.p;
    PERIOD = _preset(key);
    document.querySelectorAll(".period button").forEach(
      (x) => x.classList.toggle("active", x.dataset.p === key));
    if ($("#d-from")) { $("#d-from").value = PERIOD.dfrom; $("#d-to").value = PERIOD.dto; }
    _reloadPeriod(); hap("sel");
  };
});
["#d-from", "#d-to"].forEach((sel) => {
  const el = $(sel);
  if (el) el.onchange = () => {
    PERIOD = { dfrom: $("#d-from").value, dto: $("#d-to").value };
    document.querySelectorAll(".period button").forEach((x) => x.classList.remove("active"));
    _reloadPeriod(); hap("sel");
  };
});

// ── админ: переключатель аккаунтов ──
let ADMIN_ACCOUNTS = [];
function setupAdmin(me) {
  const bar = $("#admin-bar");
  if (!me.is_admin) { bar.classList.add("hidden"); return; }
  ADMIN_ACCOUNTS = me.accounts || [];
  bar.classList.remove("hidden");
  const cur = ADMIN_ACCOUNTS.find((a) => a.account === me.account);
  $("#admin-acc").textContent = (cur && cur.name) || me.account;
}
$("#admin-pick").onclick = () => {
  $("#pk-title").textContent = "Аккаунт (админ)";
  $("#pk-body").innerHTML = '<div class="list">' + ADMIN_ACCOUNTS.map((a) =>
    `<div class="cell tap pk-acc${a.account === VIEW_ACCOUNT ? " sel" : ""}" data-acc="${esc(a.account)}">`
    + `<span>${esc(a.name)}</span><span class="hint">${esc(a.account)}</span></div>`).join("") + "</div>";
  openSheet("#picker"); hap("sel");
  $("#pk-body").querySelectorAll(".pk-acc").forEach((el) => {
    el.onclick = () => { closeSheet("#picker"); VIEW_ACCOUNT = el.dataset.acc; hap("light"); boot(); };
  });
};

async function boot() {
  try {
    if ($("#d-from")) { $("#d-from").value = PERIOD.dfrom; $("#d-to").value = PERIOD.dto; }
    const [me, st] = await Promise.all([api("/api/me" + qp()), api("/api/settings")]);
    renderMe(me); setupAdmin(me);
    bindToggles(st.features, st.tg_connected, st.getmatch_linked, st.habr_linked, st.hh_linked);
    bindConfig(st.config, st.resumes || [], st.hh_linked, st.tg_connected);
    if ($("#hh-hint")) {
      $("#hh-hint").style.display = st.hh_linked ? "none" : "";
      $("#hh-hint").textContent = st.hh_linked ? "" : "Чтобы пользоваться функциями hh — подключите аккаунт: /addaccount в боте.";
    }
    if ($("#habr-hint")) {
      $("#habr-hint").style.display = st.habr_linked ? "none" : "";
      $("#habr-hint").textContent = st.habr_linked ? "" : "Чтобы включить — подключите Habr Career: /addaccount → Habr (логин + пароль).";
    }
    if ($("#tgch-hint")) {
      $("#tgch-hint").style.display = st.tg_connected ? "none" : "";
      $("#tgch-hint").textContent = st.tg_connected ? "" : "Чтобы включить — подключите Telegram: /connect в боте.";
    }
    renderSources(st.sources); renderGmLink(st); wireGmLink();
    $("#giga-hint").textContent = st.tg_connected
      ? ""
      : "⚠️ Чтобы включить «Авто-задачи в Telegram», дайте доступ к Telegram: команда /connect в боте.";
    loadDialogs(); loadActivity(); loadActions(); loadGiga(); loadGetmatchApps(); loadHabrApps();
    api("/api/trends").then((t) => renderTrend(t.days)).catch(() => {});
  } catch (e) {
    err(String(e.message) === "not_linked"
      ? "Сначала привяжи профиль: открой бота, нажми /start и поделись номером"
      : "Ошибка загрузки: " + e.message);
  }
}
boot();
