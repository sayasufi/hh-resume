"use strict";
const tg = window.Telegram ? window.Telegram.WebApp : null;
const INIT = tg ? tg.initData : "";
if (tg) { tg.ready(); tg.expand(); try { tg.setHeaderColor("secondary_bg_color"); } catch (e) {} }

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const err = (m) => { const e = $("#err"); e.textContent = m; e.classList.remove("hidden"); setTimeout(() => e.classList.add("hidden"), 4000); };
const hap = (k) => { try { if (!tg || !tg.HapticFeedback) return; k === "sel" ? tg.HapticFeedback.selectionChanged() : tg.HapticFeedback.impactOccurred("light"); } catch (e) {} };

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "X-Init-Data": INIT, "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (r.status === 404) throw new Error("not_linked");
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
const save = (key, value) => api("/api/settings", { method: "POST", body: JSON.stringify({ key, value }) });

// вкладки
document.querySelectorAll("#tabs button").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active"); $("#tab-" + b.dataset.tab).classList.add("active");
    window.scrollTo(0, 0); hap("sel");
  };
});

function renderMe(d) {
  const p = d.profile, s = d.stats;
  $("#avatar").textContent = (p.name || "·").trim().charAt(0).toUpperCase() || "·";
  $("#hname").textContent = p.name || "—";
  const paused = (p.status || "").includes("паузе");
  const st = $("#hstatus"); st.textContent = p.status || ""; st.className = "pill " + (paused ? "warn" : "good");
  $("#p-name").textContent = p.name || "—";
  $("#p-id").textContent = p.hh_id || "—";
  $("#p-resume").textContent = p.resume || "—";
  $("#p-salary").textContent = p.salary ? (p.salary + " ₽") : "—";
  $("#p-status").textContent = p.status || "—";
  $("#s-apps").textContent = s.applications_total;
  $("#s-today").textContent = s.applications_today;
  $("#s-views").textContent = s.resume_views;
  $("#s-resp").textContent = s.responses;
  $("#s-inv").textContent = s.invitations;
  const max = Math.max(1, ...s.funnel.map((f) => f.value));
  $("#funnel").innerHTML = s.funnel.map((f) =>
    `<div class="fbar"><div class="fill" style="width:${Math.round(f.value / max * 100)}%"></div>`
    + `<div class="ftext"><span>${esc(f.label)}</span><span class="fval"><b>${f.value}</b>`
    + `${f.conv != null ? `<em>${f.conv}%</em>` : ""}</span></div></div>`).join("");
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

function openSheet(id) { $(id).classList.remove("hidden"); }
function closeSheet(id) { $(id).classList.add("hidden"); }
document.querySelectorAll(".sheet-wrap").forEach((w) => { w.onclick = (e) => { if (e.target === w) closeSheet("#" + w.id); }; });

async function openDialog(id) {
  const d = DIALOGS.find((x) => String(x.id) === String(id));
  if (!d) return; hap("sel");
  $("#m-title").textContent = d.title;
  $("#m-emp").textContent = d.employer + " · " + d.state;
  const hh = $("#m-hh");
  if (d.url) { hh.href = d.url; hh.classList.remove("hidden"); } else hh.classList.add("hidden");
  $("#m-body").innerHTML = '<div class="empty">Загрузка…</div>';
  openSheet("#modal");
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
function bindToggles(features) {
  document.querySelectorAll(".toggle input").forEach((inp) => {
    inp.checked = !!features[inp.dataset.feat];
    inp.onchange = async () => {
      const row = inp.closest(".toggle"); row.classList.add("busy");
      try { await save(inp.dataset.feat, inp.checked); hap("light"); }
      catch (e) { inp.checked = !inp.checked; err("Не удалось сохранить"); }
      finally { row.classList.remove("busy"); }
    };
  });
}

function resumeTitle(id) { const r = RESUMES.find((x) => String(x.id) === String(id)); return r ? (r.title || r.id) : (id || "—"); }
function bindConfig(cfg, resumes) {
  RESUMES = resumes || []; RESUME_ID = cfg.resume_id || (RESUMES[0] && RESUMES[0].id) || "";
  $("#cfg-salary").value = cfg.salary || "";
  $("#cfg-limit").value = cfg.max_per_day != null ? cfg.max_per_day : "";
  $("#cfg-tlimit").value = cfg.tests_per_day != null ? cfg.tests_per_day : "";
  $("#resume-val").textContent = resumeTitle(RESUME_ID);
  const wire = (el, key, conv) => {
    el.onchange = async () => {
      el.classList.add("busy");
      try { await save(key, conv ? conv(el.value) : el.value); hap("light"); }
      catch (e) { err("Не удалось сохранить"); } finally { el.classList.remove("busy"); }
    };
  };
  wire($("#cfg-salary"), "salary");
  wire($("#cfg-limit"), "apply.max_per_day", (v) => parseInt(v || "0", 10));
  wire($("#cfg-tlimit"), "apply.tests_per_day", (v) => parseInt(v || "0", 10));
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

// период (30/90/Всё) — общий для статистики и откликов
let PERIOD = 90;
const loadStats = (d) => api("/api/me?days=" + d).then(renderMe).catch(() => {});
const loadDialogs = (d) => api("/api/dialogs?days=" + d)
  .then((r) => { DIALOGS = r.items || []; renderDialogs(); }).catch(() => {});
document.querySelectorAll(".period button").forEach((b) => {
  b.onclick = () => {
    PERIOD = parseInt(b.dataset.p, 10);
    document.querySelectorAll(".period button").forEach(
      (x) => x.classList.toggle("active", x.dataset.p === b.dataset.p));
    loadStats(PERIOD); loadDialogs(PERIOD); hap("sel");
  };
});

(async () => {
  try {
    const [me, st] = await Promise.all([api("/api/me?days=" + PERIOD), api("/api/settings")]);
    renderMe(me); bindToggles(st.features); bindConfig(st.config, st.resumes || []);
    loadDialogs(PERIOD);
    api("/api/trends").then((t) => renderTrend(t.days)).catch(() => {});
  } catch (e) {
    err(String(e.message) === "not_linked"
      ? "Сначала привяжи профиль: в боте /link и поделись номером"
      : "Ошибка загрузки: " + e.message);
  }
})();
