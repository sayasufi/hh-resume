"use strict";
const tg = window.Telegram ? window.Telegram.WebApp : null;
const INIT = tg ? tg.initData : "";
if (tg) { tg.ready(); tg.expand(); }

const $ = (s) => document.querySelector(s);
const err = (msg) => { const e = $("#err"); e.textContent = msg; e.classList.remove("hidden"); };

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "X-Init-Data": INIT, "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  if (r.status === 404) throw new Error("not_linked");
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

// ── вкладки ──
document.querySelectorAll("#tabs button").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("#tab-" + b.dataset.tab).classList.add("active");
    if (tg) tg.HapticFeedback && tg.HapticFeedback.selectionChanged();
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
    + `<div class="ftext"><span>${f.label}</span><b>${f.value}</b></div></div>`).join("");
}

function bindToggles(feat) {
  document.querySelectorAll(".toggle input").forEach((inp) => {
    inp.checked = !!feat[inp.dataset.feat];
    inp.onchange = async () => {
      const row = inp.closest(".toggle");
      row.classList.add("busy");
      try {
        await api("/api/settings", {
          method: "POST",
          body: JSON.stringify({ key: inp.dataset.feat, value: inp.checked }),
        });
        if (tg) tg.HapticFeedback && tg.HapticFeedback.impactOccurred("light");
      } catch (e) {
        inp.checked = !inp.checked;
        err("Не удалось сохранить");
      } finally {
        row.classList.remove("busy");
      }
    };
  });
}

(async () => {
  try {
    const [me, feat] = await Promise.all([api("/api/me"), api("/api/settings")]);
    renderMe(me);
    bindToggles(feat);
  } catch (e) {
    if (String(e.message) === "not_linked") {
      err("Сначала привяжи профиль: в боте набери /link и поделись номером");
    } else {
      err("Ошибка загрузки: " + e.message);
    }
  }
})();
