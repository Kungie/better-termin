const $ = (s) => document.querySelector(s);
const WD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
// Line icons (24×24, drawn with the button's text colour)
const icon = (paths) => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${paths}</svg>`;
const ICONS = {
  bell: icon('<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>'),
  check: icon('<path d="M20 6 9 17l-5-5"/>'),
  mail: icon('<rect x="3" y="5" width="18" height="14" rx="1"/><path d="m3 7 9 6 9-6"/>'),
};
let data = null, timer = null;
// Alerts created in this browser: category id -> {token, status: "pending" | "active"}.
// Only the tokens are kept locally; the status comes from the server.
let myAlerts = new Map();
// Groups start collapsed; the ones the viewer opens are remembered.
let openGroups = new Set();
try { openGroups = new Set(JSON.parse(load("openGroups") || "[]")); } catch {}

function store(k, v) { try { v === undefined ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch {} }
function load(k) { try { return localStorage.getItem(k); } catch { return null; } }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

const pad = (n) => String(n).padStart(2, "0");
function fmtDate(iso) { const [y, m, d] = iso.split("-"); return `${d}.${m}.${y}`; }
function fmtDay(iso) { const d = new Date(iso); return `${pad(d.getDate())}.${pad(d.getMonth()+1)}.${d.getFullYear()}`; }
function fmtDT(iso) { const d = new Date(iso); return `${pad(d.getDate())}.${pad(d.getMonth()+1)}. ${pad(d.getHours())}:${pad(d.getMinutes())}`; }
function fmtTime(iso) { const d = new Date(iso); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; }
// [count, unit] for a duration, e.g. [2, "day"]
function durParts(sec) {
  const m = sec / 60, h = m / 60, d = h / 24;
  if (m < 60) return [Math.max(1, Math.round(m)), "minute"];
  if (h < 36) return [Math.round(h), "hour"];
  if (d < 14) return [Math.round(d), "day"];
  return [Math.round(d / 7), "week"];
}
function dur(sec) { const [n, u] = durParts(sec); return `${n} ${u}${n === 1 ? "" : "s"}`; }
function every(sec) { const [n, u] = durParts(sec); return n === 1 ? `~every ${u}` : `~every ${n} ${u}s`; }
function ago(iso) { return dur((Date.now() - new Date(iso)) / 1000) + " ago"; }
function daysUntil(dateIso) {
  const t = new Date(); t.setHours(0,0,0,0);
  const d = new Date(dateIso + "T00:00:00");
  const n = Math.round((d - t) / 86400000);
  return n <= 0 ? "today" : n === 1 ? "tomorrow" : `in ${n} days`;
}

function freqHtml(c) {
  const s = c.stats;
  if (!s.openings) {
    return `<div class="freq">No new slots have opened since tracking started (${fmtDay(data.tracking_since)}).</div>
            <div class="sub">Opening frequency will appear here as data is collected.</div>`;
  }
  const parts = [];
  if (s.avg_interval_s) parts.push(`New slots open: <b>${every(s.avg_interval_s)}</b>`);
  const sub = [`last opened ${ago(s.last_opened)} (${fmtDT(s.last_opened)})`];
  if (s.typical_hour !== null) sub.push(`usually around ${pad(s.typical_hour)}:00`);
  if (s.avg_open_s) sub.push(`stays open ~${dur(s.avg_open_s)}`);
  sub.push(`${s.openings_30d}× in the last 30 days`);
  return `<div class="freq">${parts.join("")}</div><div class="sub">${sub.join(" · ")}</div>`;
}

function rowHtml(c) {
  let status;
  if (c.error && !c.earliest) {
    status = `<span class="badge err">Check failed</span><div class="sub">${esc(c.error)}</div>`;
  } else if (!c.checked_at) {
    status = `<span class="badge none">Waiting for check…</span>`;
  } else if (c.earliest) {
    const d = new Date(c.earliest.date + "T00:00:00");
    const extra = [];
    if (c.available_since) extra.push(`opened ${ago(c.available_since)}`);
    if (c.stats.earlier_30d) extra.push(`an earlier slot appeared ${c.stats.earlier_30d}× in the last 30 days`);
    status = `<span class="badge ok">Available · ${daysUntil(c.earliest.date)}</span>
      <div class="when">${fmtDate(c.earliest.date)} <span class="wd">${WD[d.getDay()]}</span> <span class="t">${esc(c.earliest.time)}</span></div>
      ${extra.length ? `<div class="sub">${extra.join(" · ")}</div>` : ""}`;
  } else {
    status = `<span class="badge none">No free slots right now</span>${freqHtml(c)}`;
  }
  const a = myAlerts.get(c.id);
  const label = !a ? `${ICONS.bell}E-mail alert` : a.status === "active" ? `${ICONS.check}Alert set` : `${ICONS.mail}Confirm in inbox`;
  return `<li class="row">
    <div><div class="cat">${esc(c.name)}</div><div class="loc" title="${esc(c.location || "")}">${esc((c.location || "").split(",")[0].replace(/^(Ausländeramt Aachen|StädteRegion Aachen)(\s*-\s*|\s+)?/, ""))}</div></div>
    <div>${status}</div>
    <div class="alert">
      <button class="btn small ${a ? (a.status === "active" ? "on" : "pending") : ""}" data-alert="${esc(c.id)}">${label}</button>
      <a class="btn small outline" href="${esc(c.booking_url)}" target="_blank" rel="noopener" title="Opens the booking site; pick “${esc(c.name)}” there">Go to site ↗</a>
    </div>
  </li>`;
}

function groupSummary(items) {
  return `${items.filter(c => c.earliest).length}/${items.length} available`;
}

function render() {
  if (!data) return;
  const cats = data.categories;
  const avail = cats.filter(c => c.earliest).length;
  $("#s-avail").textContent = cats.length ? `${avail} of ${cats.length} categories` : "–";
  $("#s-last").textContent = data.last_check ? `${fmtTime(data.last_check)} (${ago(data.last_check)})` : "not yet";
  $("#s-next").textContent = data.progress.running ? "checking now" : (data.next_check ? fmtTime(data.next_check) : "–");
  $("#s-since").textContent = fmtDay(data.tracking_since);

  const p = data.progress, bar = $("#progress");
  bar.classList.toggle("on", p.running);
  bar.firstElementChild.style.width = p.total ? `${(p.done / p.total) * 100}%` : "0";
  $("#hint").textContent = p.running ? `Checking ${p.done}/${p.total}: ${p.current || ""}` : (data.error ? `Last check failed: ${data.error}` : "");
  $("#refresh").disabled = p.running;

  if (!cats.length) { $("#list").innerHTML = `<div class="empty">Running the first check…</div>`; return; }

  // office (Ausländeramt, Zulassungsstelle …) → group → categories, in server order
  const offices = [];
  for (const c of cats) {
    let o = offices.find(o => o.name === c.office);
    if (!o) offices.push(o = { name: c.office, groups: [] });
    let g = o.groups.find(g => g.name === c.group);
    if (!g) o.groups.push(g = { name: c.group, key: `${c.office} / ${c.group}`, items: [] });
    g.items.push(c);
  }
  // Ausländeramt groups were remembered by their bare name before offices were added
  const isOpen = (o, g) => openGroups.has(g.key) || (o.name === "Ausländeramt" && openGroups.has(g.name));
  $("#list").innerHTML = offices.map(o => `
    <section class="office">
      <h2 class="office-head"><span>${esc(o.name)}</span>
        <span class="cnt">${groupSummary(o.groups.flatMap(g => g.items))}</span></h2>
      ${o.groups.map(g => `
      <details class="group" data-group="${esc(g.key)}" ${isOpen(o, g) ? "open" : ""}>
        <summary class="group-head"><span class="gname">${esc(g.name === o.name ? "All services" : g.name)}</span>
          <span class="cnt">${groupSummary(g.items)}</span></summary>
        <ul class="rows">${g.items.map(rowHtml).join("")}</ul>
      </details>`).join("")}
    </section>`).join("");
}

async function refreshStatus() {
  try {
    const r = await fetch("/api/status");
    data = await r.json();
    render();
  } catch (e) {
    $("#hint").textContent = "Can't reach the server.";
  }
  clearTimeout(timer);
  timer = setTimeout(refreshStatus, data && data.progress.running ? 3000 : 30000);
}

function saveTokens() {
  store("alertTokens", JSON.stringify(Object.fromEntries([...myAlerts].map(([id, a]) => [id, a.token]))));
}

async function loadAlerts() {
  let tokens = {};
  try { tokens = JSON.parse(load("alertTokens") || "{}"); } catch {}
  const list = Object.values(tokens);
  if (!list.length) return;
  try {
    const r = await fetch("/api/alerts?tokens=" + encodeURIComponent(list.join(",")));
    const rows = await r.json();
    // tokens the server no longer knows (removed or expired) are dropped
    myAlerts = new Map(rows.map(a => [a.category_id, { token: a.token, status: a.status }]));
    saveTokens();
    render();
  } catch {}
}

// ---- remember which groups are open across re-renders and reloads
$("#list").addEventListener("toggle", (e) => {
  const g = e.target.dataset && e.target.dataset.group;
  if (g === undefined) return;
  e.target.open ? openGroups.add(g) : openGroups.delete(g);
  store("openGroups", JSON.stringify([...openGroups]));
}, true);

// ---- manual refresh
$("#refresh").addEventListener("click", async () => {
  $("#refresh").disabled = true;
  const r = await fetch("/api/refresh", { method: "POST" });
  if (r.status === 429) $("#hint").textContent = "Just checked — please wait a moment.";
  setTimeout(refreshStatus, 800);
});

// ---- alert dialog
const dlg = $("#dlg");
let dlgCat = null, dlgMode = "new";  // "new" | "manage" | "done"

function showDialog(mode, msg = "") {
  dlgMode = mode;
  const a = myAlerts.get(dlgCat.id);
  $("#dlg-cat").textContent = dlgCat.name;
  $("#dlg-new").hidden = mode !== "new";
  $("#dlg-msg").hidden = mode === "new";
  $("#dlg-msg").textContent = msg;
  $("#dlg-err").textContent = "";
  $("#dlg-title").textContent = mode === "new" ? "E-mail alert" : !a ? "E-mail alert" : a.status === "active" ? "Alert set" : "Waiting for confirmation";
  $("#dlg-ok").hidden = mode === "done";
  $("#dlg-ok").textContent = mode === "new" ? "Set alert" : "Remove alert";
  $("#dlg-cancel").textContent = mode === "done" ? "Close" : "Cancel";
  $(".m-foot").classList.toggle("single", mode === "done");
}

$("#list").addEventListener("click", (e) => {
  const b = e.target.closest("[data-alert]");
  if (!b) return;
  dlgCat = data.categories.find(c => c.id === b.dataset.alert);
  const a = myAlerts.get(dlgCat.id);
  if (!a && !data.alerts_enabled) {
    showDialog("done", "E-mail alerts aren't available yet. They'll be switched on soon.");
  } else if (!a) {
    $("#dlg-email").value = load("alertEmail") || "";
    $("#dlg-consent").checked = false;
    showDialog("new");
  } else {
    showDialog("manage", a.status === "active"
      ? "This alert is active. You'll get an e-mail when a slot opens or an earlier slot appears."
      : "We sent you a confirmation link. The alert starts once you click it. Unconfirmed sign-ups are deleted after 48 hours.");
  }
  dlg.showModal();
  if (!a) $("#dlg-email").focus();
});
const close = () => dlg.close();
$("#dlg-x").addEventListener("click", close);
$("#dlg-cancel").addEventListener("click", close);

async function api(method, body) {
  const r = await fetch("/api/alerts", { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return [r, await r.json().catch(() => ({}))];
}

$("#dlg-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (dlgMode === "manage") {
    const [r] = await api("DELETE", { token: myAlerts.get(dlgCat.id).token });
    if (!r.ok && r.status !== 404) { $("#dlg-err").textContent = "Something went wrong."; return; }
    myAlerts.delete(dlgCat.id); saveTokens(); close(); render();
    return;
  }
  const email = $("#dlg-email").value.trim();
  if (!$("#dlg-consent").checked) { $("#dlg-err").textContent = "Please accept the privacy policy to set an alert."; return; }
  const [r, res] = await api("POST", { email, category_id: dlgCat.id, consent: true });
  if (!r.ok) { $("#dlg-err").textContent = res.error || "Something went wrong."; return; }
  store("alertEmail", email);
  if (res.token) {
    myAlerts.set(dlgCat.id, { token: res.token, status: "pending" }); saveTokens(); render();
    showDialog("done", `Check your inbox: we sent a confirmation link to ${email}. The alert starts once you click it.`);
  } else if (res.status === "active") {
    showDialog("done", "This address already has an active alert for this category. To remove it, use the link in any alert e-mail.");
  } else {
    showDialog("done", "This address is waiting for confirmation. We've sent the confirmation e-mail again (at most every 10 minutes).");
  }
});

refreshStatus();
loadAlerts();
setInterval(render, 60000); // keep "x minutes ago" fresh
