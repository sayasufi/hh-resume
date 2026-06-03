"use strict";
const tg = window.Telegram ? window.Telegram.WebApp : null;
const INIT = tg ? tg.initData : "";
if (tg) { tg.ready(); tg.expand(); }

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const err = (msg) => { const e = $("#err"); e.textContent = msg; e.classList.remove("hidden"); };
const haptic = (k) => { try { tg && tg.HapticFeedback && tg.HapticFeedback[k === "sel" ? "selectionChanged" : "impactOccurred"](k === "sel" ? undefined : "light"); } catch (e) {} };

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "X-Init-Data": INIT, "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  if (r.status === 404) throw new Error("not_linked");
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
const save = (key, value) => api("/api/settings", { method: "POST", body: JSON.stringify({ key, value }) });

// ── вкладки ──
document.querySelectorAll("#tabs button").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("#tab-" + b.dataset.tab).classList.add("active");
    haptic("sel");
  };
});

function renderMe(d) {
  const p = d.profile, s = d.stats;
  $("#hname").textContent = p.name || "—";
  $("#hstatus").textContent = p.status || "";
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
  $("#s-intv").textContent = s.interviews;
  const max = Math.max(1, ...s.funnel.map((f) => f.value));
  $("#funnel").innerHTML = s.funnel.map((f) =>
    `<div class="fbar"><div class="fill" style="width:${Math.round(f.value / max * 100)}%"></div>`
    + `<div class="ftext"><span>${esc(f.label)}</span><b>${f.value}</b></div></div>`).join("");
}

function renderTrend(days) {
  const box = $("#trend");
  if (!days || days.length < 2) {
    box.innerHTML = '<div class="hint" style="padding:8px 4px">Данные копятся по дням — график появится за пару дней использования.</div>';
    return;
  }
  const vals = days.map((d) => d.applications);
  const max = Math.max(1, ...vals);
  const W = 320, H = 90, n = days.length;
  const pts = vals.map((v, i) => [i * (W / (n - 1)), H - (v / max) * (H - 10) - 5]);
  const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  const dots = pts.map((p) => `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="2.5"/>`).join("");
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="chart" preserveAspectRatio="none">`
    + `<path d="${line}" fill="none"/>${dots}</svg>`
    + `<div class="chart-x"><span>${esc(days[0].day.slice(5))}</span><span>${esc(days[n - 1].day.slice(5))}</span></div>`;
}

function renderDialogs(items) {
  const box = $("#dialogs");
  if (!items || !items.length) {
    box.innerHTML = '<div class="hint" style="padding:8px 4px">Откликов пока нет.</div>';
    return;
  }
  box.innerHTML = items.map((d) =>
    `<div class="dlg"><div class="dlg-top"><span class="dlg-st">${d.emoji} ${esc(d.state)}</span>`
    + `<span class="dlg-date">${esc(d.updated)}</span></div>`
    + `<div class="dlg-title">${esc(d.title)}</div>`
    + `<div class="dlg-emp">${esc(d.employer)}</div></div>`).join("");
}

function bindToggles(features) {
  document.querySelectorAll(".toggle input").forEach((inp) => {
    inp.checked = !!features[inp.dataset.feat];
    inp.onchange = async () => {
      const row = inp.closest(".toggle"); row.classList.add("busy");
      try { await save(inp.dataset.feat, inp.checked); haptic("light"); }
      catch (e) { inp.checked = !inp.checked; err("Не удалось сохранить"); }
      finally { row.classList.remove("busy"); }
    };
  });
}

function bindConfig(cfg, resumes) {
  $("#cfg-salary").value = cfg.salary || "";
  $("#cfg-limit").value = cfg.max_per_day != null ? cfg.max_per_day : "";
  const sel = $("#cfg-resume");
  sel.innerHTML = resumes.map((r) =>
    `<option value="${esc(r.id)}">${esc(r.title || r.id)}</option>`).join("")
    || '<option value="">— нет резюме —</option>';
  if (cfg.resume_id) sel.value = cfg.resume_id;
  const wire = (el, key, conv) => {
    el.onchange = async () => {
      el.classList.add("busy");
      try { await save(key, conv ? conv(el.value) : el.value); haptic("light"); }
      catch (e) { err("Не удалось сохранить"); }
      finally { el.classList.remove("busy"); }
    };
  };
  wire($("#cfg-salary"), "salary");
  wire($("#cfg-limit"), "apply.max_per_day", (v) => parseInt(v || "0", 10));
  wire($("#cfg-resume"), "apply.resume_id");
}

(async () => {
  try {
    const [me, st] = await Promise.all([api("/api/me"), api("/api/settings")]);
    renderMe(me);
    bindToggles(st.features);
    bindConfig(st.config, st.resumes || []);
    // не блокируем первый экран — диалоги и тренды догружаем
    api("/api/dialogs").then((d) => renderDialogs(d.items)).catch(() => {});
    api("/api/trends").then((t) => renderTrend(t.days)).catch(() => {});
  } catch (e) {
    if (String(e.message) === "not_linked") {
      err("Сначала привяжи профиль: в боте набери /link и поделись номером");
    } else {
      err("Ошибка загрузки: " + e.message);
    }
  }
})();
