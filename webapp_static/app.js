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
  const p = d.profile, s = d.stats;
  $("#avatar").textContent = (p.name || "·").trim().charAt(0).toUpperCase() || "·";
  $("#hname").textContent = p.name || "—";
  const stt = p.status || "";
  const st = $("#hstatus"); st.textContent = stt;
  const sk = p.status_kind || (stt.includes("работает") ? "ok" : stt.indexOf("всё") === 0 ? "off" : "paused");
  st.className = "pill " + (sk === "ok" ? "good" : sk === "off" ? "bad" : "warn");
  $("#p-name").textContent = p.name || "—";
  $("#p-id").textContent = p.hh_id || "—";
  $("#p-resume").textContent = p.resume || "—";
  $("#p-salary").textContent = p.salary ? (p.salary + " ₽") : "—";
  $("#p-status").textContent = p.status || "—";
  const max = Math.max(1, ...s.funnel.map((f) => f.value));
  $("#funnel").innerHTML = s.funnel.map((f) =>
    `<div class="fbar"><div class="fill" style="width:${Math.round(f.value / max * 100)}%"></div>`
    + `<div class="ftext"><span>${esc(f.label)}</span><span class="fval"><b>${f.value}</b>`
    + `${f.conv != null ? `<em>${f.conv}%</em>` : ""}</span></div></div>`).join("");
  const bd = s.breakdown || [];
  $("#breakdown").innerHTML = bd.length ? bd.map((b) =>
    `<div class="cell"><span class="k">${b.emoji} ${esc(b.label)}</span>`
    + `<span class="v"><b>${b.value}</b><em style="color:var(--hint);font-weight:400;margin-left:6px">${b.pct}%</em></span></div>`).join("")
    : '<div class="empty">Нет данных за период</div>';
  $("#next-apply").textContent = d.next_apply
    ? "⏱ Следующие обычные отклики: " + d.next_apply
    : "⏸ Обычные отклики на паузе — включи «Авто-отклики» в Функциях.";
}

function renderTrend(days) {
  const box = $("#trend");
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
function bindToggles(features, tgConnected) {
  document.querySelectorAll(".toggle input[data-feat]").forEach((inp) => {
    inp.checked = !!features[inp.dataset.feat];
    // ГигаРекрутер нельзя включить без подключённого Telegram
    const lockGiga = inp.dataset.feat === "giga" && !tgConnected;
    inp.disabled = lockGiga;
    if (lockGiga) inp.checked = false;
    inp.closest(".toggle").classList.toggle("disabled", lockGiga);
    inp.onchange = async () => {
      const row = inp.closest(".toggle"); row.classList.add("busy");
      try { await save(inp.dataset.feat, inp.checked); hap("light"); }
      catch (e) { inp.checked = !inp.checked; err((e && e.message) || "Не удалось сохранить"); }
      finally { row.classList.remove("busy"); }
    };
  });
}

function resumeTitle(id) { const r = RESUMES.find((x) => String(x.id) === String(id)); return r ? (r.title || r.id) : (id || "—"); }
function bindConfig(cfg, resumes) {
  RESUMES = resumes || []; RESUME_ID = cfg.resume_id || (RESUMES[0] && RESUMES[0].id) || "";
  const capL = cfg.max_per_day_cap || 200, capT = cfg.tests_per_day_cap || 30;
  $("#cfg-salary").value = cfg.salary || "";
  $("#cfg-limit").value = cfg.max_per_day != null ? cfg.max_per_day : "";
  $("#cfg-tlimit").value = cfg.tests_per_day != null ? cfg.tests_per_day : "";
  $("#cfg-limit").max = capL; $("#cfg-tlimit").max = capT;
  if ($("#cap-limit")) $("#cap-limit").textContent = "(макс " + capL + ")";
  if ($("#cap-tlimit")) $("#cap-tlimit").textContent = "(макс " + capT + ")";
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
  clampWire($("#cfg-tlimit"), "apply.tests_per_day", capT);
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
  const box = $("#giga-card");
  if (!g || (!g.pending && !g.done && !g.active)) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const last = g.last && g.last.vacancy
    ? `<div class="giga-last">Последнее: ${esc(g.last.vacancy)} · ${esc(g.last.at)}</div>` : "";
  box.innerHTML = '<div class="giga-h">🤖 ГигаРекрутер сам проходит интервью</div>'
    + '<div class="giga-row">'
    + `<span class="gnum"><b>${g.done | 0}</b> пройдено</span>`
    + `<span class="gnum"><b>${g.pending | 0}</b> в очереди</span>`
    + (g.active ? `<span class="gnum"><b>${g.active | 0}</b> сейчас</span>` : "")
    + '</div>' + last;
}
const loadGiga = () => api("/api/giga").then(renderGiga).catch(() => {});

// дела (что нужно сделать самому)
function renderActions(items) {
  const box = $("#actions");
  $("#act-count").textContent = items.length;
  if (!items.length) { box.innerHTML = '<div class="empty">Дел нет — всё под контролем 👌</div>'; return; }
  box.innerHTML = '<div class="list">' + items.map((a) =>
    `<div class="cell act"><div class="dlg-main">`
    + `<div class="dlg-title">${esc(a.action)}</div>`
    + `<div class="dlg-emp">${esc(a.vacancy)}</div>`
    + `<div class="dlg-date">${esc(a.created_at)}</div></div>`
    + `<div class="act-btns">`
    + (a.action_url ? `<button class="abtn open" data-url="${esc(a.action_url)}">Открыть ↗</button>` : "")
    + (a.chat_url ? `<button class="abtn chat" data-url="${esc(a.chat_url)}">Чат</button>` : "")
    + `<button class="abtn done" data-id="${a.id}">✓</button></div></div>`).join("") + "</div>";
  box.querySelectorAll(".abtn[data-url]").forEach((el) => {
    el.onclick = () => { hap("sel"); if (tg && tg.openLink) tg.openLink(el.dataset.url); else window.open(el.dataset.url, "_blank"); };
  });
  box.querySelectorAll(".abtn.done").forEach((el) => {
    el.onclick = async () => {
      const row = el.closest(".act"); row.style.opacity = ".4";
      try { await api("/api/action_done", { method: "POST", body: JSON.stringify({ id: parseInt(el.dataset.id, 10) }) }); hap("light"); loadActions(); }
      catch (e) { err("Не удалось"); row.style.opacity = "1"; }
    };
  });
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
  loadStats(); loadActivity(); loadDialogs(); loadActions(); loadGiga();
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
    bindToggles(st.features, st.tg_connected); bindConfig(st.config, st.resumes || []);
    $("#giga-hint").textContent = st.tg_connected
      ? "✅ Telegram подключён — ГигаРекрутер сам проходит интервью за вас."
      : "⚠️ Чтобы включить ГигаРекрутера, подключите Telegram: команда /connect в боте.";
    loadDialogs(); loadActivity(); loadActions(); loadGiga();
    api("/api/trends").then((t) => renderTrend(t.days)).catch(() => {});
  } catch (e) {
    err(String(e.message) === "not_linked"
      ? "Сначала привяжи профиль: в боте /link и поделись номером"
      : "Ошибка загрузки: " + e.message);
  }
}
boot();
