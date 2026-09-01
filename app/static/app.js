"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// File names come from the board's directory listing and an uploaded .bin may be
// called anything — an unescaped quote used to cut the row's markup in half.
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---------- i18n ----------
let LOCALE = "en";
let T = window.I18N.en;
const t = (key) => (T && T[key]) || window.I18N.en[key] || key;
const pname = (ch) => T["param." + ch.key] || window.I18N.en["param." + ch.key] || ch.name;

function applyLocale(loc) {
  LOCALE = window.I18N[loc] ? loc : "en";
  T = window.I18N[LOCALE];
  document.documentElement.lang = LOCALE;
  $$("[data-i18n]").forEach((el) => (el.textContent = t(el.dataset.i18n)));
  $$("[data-i18n-ph]").forEach((el) => (el.placeholder = t(el.dataset.i18nPh)));
  const ls = $("#localeSelect");
  if (ls) ls.value = LOCALE;
  if (catalog.length) renderParams();
  if (lastSnapshot) applySnapshot(lastSnapshot);
}

// ---------- tabs ----------
$$(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".tab").forEach((b) => {
      const on = b === btn;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    // the tab strip scrolls on a phone — keep the one you just picked in view
    btn.scrollIntoView({ inline: "center", block: "nearest", behavior: "smooth" });
    const name = btn.dataset.tab;
    $$(".tabpanel").forEach((p) => p.classList.toggle("is-active", p.id === "tab-" + name));
    fwLeave();
    if (name === "logs") loadLogs();
    if (name === "config") { loadConfig(); loadTime(); loadFwLogs(); }
    if (name === "firmware") fwEnter();
    if (name === "testing") testingEnter();
  });
});

// ---------- helpers ----------
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).error || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}
// ---------- feedback: toast, confirm dialog, busy button ----------
// Native alert()/confirm() block the poll loop's rendering, come up in the
// system locale (not the one picked in the UI) and make "file too large" look
// exactly like "erase the ECU's fault memory". Both are replaced by these.
function toast(msg, kind = "err", ms = 5000) {
  let box = $("#toasts");
  if (!box) {
    box = document.createElement("div");
    box.id = "toasts";
    box.className = "toasts";
    box.setAttribute("role", "status");
    box.setAttribute("aria-live", "polite");
    document.body.appendChild(box);
  }
  const el = document.createElement("div");
  el.className = "toast toast--" + kind;
  el.textContent = msg;
  el.addEventListener("click", () => el.remove());
  box.appendChild(el);
  setTimeout(() => el.remove(), ms);
  return el;
}
const toastErr = (err) => toast(t("banner.error") + " " + (err && err.message ? err.message : err), "err");

// Resolves true/false. okLabel carries the verb of the action ("Write to ECU"),
// so the button says what it does instead of "OK".
function confirmDialog(text, { danger = false, okLabel } = {}) {
  return new Promise((resolve) => {
    const back = document.createElement("div");
    back.className = "modal-back";
    back.innerHTML =
      `<div class="modal" role="alertdialog" aria-modal="true">` +
      `<p class="modal__text"></p>` +
      `<div class="modal__actions">` +
      `<button class="btn btn--ghost" data-act="no"></button>` +
      `<button class="btn ${danger ? "btn--danger" : "btn--primary"}" data-act="yes"></button>` +
      `</div></div>`;
    $(".modal__text", back).textContent = text;
    $('[data-act="no"]', back).textContent = t("ui.cancel");
    $('[data-act="yes"]', back).textContent = okLabel || t("ui.confirm");
    const prev = document.activeElement;
    const done = (v) => {
      document.removeEventListener("keydown", onKey, true);
      back.remove();
      if (prev && prev.focus) prev.focus();
      resolve(v);
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); done(false); return; }
      if (e.key !== "Tab") return;
      const btns = $$("button", back);                      // keep focus inside the dialog
      const i = btns.indexOf(document.activeElement);
      e.preventDefault();
      btns[(i + (e.shiftKey ? btns.length - 1 : 1)) % btns.length].focus();
    };
    back.addEventListener("click", (e) => { if (e.target === back) done(false); });
    $('[data-act="no"]', back).addEventListener("click", () => done(false));
    $('[data-act="yes"]', back).addEventListener("click", () => done(true));
    document.addEventListener("keydown", onKey, true);
    document.body.appendChild(back);
    $('[data-act="no"]', back).focus();                     // safe default under the thumb
  });
}

// One busy state for every command that goes to the board: the button locks and
// says it is working, so a second tap cannot queue a second K-Line command.
async function withBusy(btn, fn) {
  if (!btn) return fn();
  if (btn.dataset.busy) return;
  btn.dataset.busy = "1";
  const wasDisabled = btn.disabled;
  btn.disabled = true;
  btn.classList.add("is-busy");
  try { return await fn(); }
  finally {
    delete btn.dataset.busy;
    btn.classList.remove("is-busy");
    btn.disabled = wasDisabled;
  }
}

// The board serves this UI over plain http:// from its own AP, and outside a
// secure context navigator.clipboard is simply absent on a phone — the hidden
// textarea + execCommand path is the only one that copies there. iOS ignores
// .select() on a readonly field, so the selection is made with a Range.
async function copyToClipboard(text) {
  if (!text) return false;
  if (window.isSecureContext && navigator.clipboard) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) {}
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0";
  document.body.appendChild(ta);
  let ok = false;
  try {
    ta.contentEditable = "true";
    const sel = window.getSelection();
    if (sel) {
      const range = document.createRange();
      range.selectNodeContents(ta);
      sel.removeAllRanges();
      sel.addRange(range);
    }
    ta.setSelectionRange(0, text.length);
    ok = document.execCommand("copy");
  } catch (e) { ok = false; }
  ta.remove();
  return !!ok;
}

function setNested(obj, path, val) {
  const keys = path.split(".");
  let o = obj;
  for (let i = 0; i < keys.length - 1; i++) o = o[keys[i]] ??= {};
  o[keys[keys.length - 1]] = val;
}
function getNested(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
}
function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
  return (n / 1073741824).toFixed(2) + " GB";
}
function fmtDate(mtime) {
  if (!mtime) return "";
  const d = new Date(mtime * 1000);
  return d.getFullYear() > 1970
    ? d.toLocaleString([], { year: "2-digit", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
    : "";
}
function setDiskFree(el, d) {
  if (!el) return;
  if (!d || !d.total) { el.innerHTML = ""; return; }
  const freePct = Math.max(0, Math.min(100, Math.round((d.free / d.total) * 100)));
  el.innerHTML = `<span>${t("disk.free")}: ${fmtSize(d.free)} / ${fmtSize(d.total)}</span>` +
    `<span class="bar"><i style="width:${freePct}%"></i></span>`;
}

// ---------- live state ----------
let catalog = [];
let statusMaps = {};        // config/status_maps.json — decodes status channels to text
let selectedKeys = new Set();
let lastSnapshot = null;

function mkParamRow(ch) {
  const row = document.createElement("label");
  row.className = "prow" + (selectedKeys.has(ch.key) ? "" : " off");
  row.dataset.key = ch.key;
  row.innerHTML =
    `<input type="checkbox" ${selectedKeys.has(ch.key) ? "checked" : ""} />` +
    `<span class="pname">${pname(ch)}</span>` +
    `<canvas class="spark"></canvas>` +
    `<span class="pval">—<span class="unit">${ch.unit || ""}</span></span>`;
  $("input", row).addEventListener("change", onSelectChange);
  return row;
}
function renderParams() {
  const box = $("#params");
  box.innerHTML = "";
  const named = catalog.filter((c) => c.default);
  const unknown = catalog.filter((c) => !c.default);
  named.forEach((ch) => box.appendChild(mkParamRow(ch)));
  if (unknown.length) {                       // collapse the long unidentified-rli list
    const hdr = document.createElement("button");
    hdr.type = "button";
    hdr.className = "unknown-toggle";
    const wrap = document.createElement("div");
    wrap.className = "unknown-wrap";
    wrap.hidden = !unknown.some((c) => selectedKeys.has(c.key));  // open if any ticked
    const relabel = () => (hdr.textContent = `${wrap.hidden ? "▸" : "▾"} ${t("params.unknown")} (${unknown.length})`);
    hdr.addEventListener("click", () => { wrap.hidden = !wrap.hidden; relabel(); });
    relabel();
    unknown.forEach((ch) => wrap.appendChild(mkParamRow(ch)));
    box.appendChild(hdr);
    box.appendChild(wrap);
  }
  if (lastSnapshot) updateValues(lastSnapshot.values);
  applyParamMode();
}

function decodeStatus(ch, val) {
  const m = statusMaps[ch.map];
  if (!m || val == null) return null;
  const base = m.base || 16;
  const type = ch.map_type || m.type || "enum";
  const entries = Object.entries(m.values || {});
  if (type === "bits") {
    const hits = entries.filter(([k]) => (val & parseInt(k, base)) === parseInt(k, base)
                                         && parseInt(k, base) !== 0);
    return hits.length ? hits.map(([, v]) => v).join(", ") : null;
  }
  const hit = entries.find(([k]) => parseInt(k, base) === val);
  return hit ? hit[1] : null;
}

function updateValues(values) {
  $$("#params .prow").forEach((row) => {
    const on = $("input", row).checked;
    const val = values[row.dataset.key];
    const span = $(".pval", row);
    const unit = span.querySelector(".unit")?.outerHTML || "";
    const ch = catalog.find((c) => c.key === row.dataset.key);
    const text = on && ch && ch.map ? decodeStatus(ch, val) : null;
    if (text) {                       // status channel: show the label, keep the raw
      span.innerHTML = `<span class="pstatus" title="${val}">${text}</span>`;
    } else {
      span.innerHTML = (on && val != null ? val : "—") + unit;
    }
  });
}

// per-parameter rolling history (last 3 s) for the inline sparkline
const spark = {};
const SPARK_MS = 3000;
const isLoggerActive = () => $("#tab-logger").classList.contains("is-active");

function pushSpark(values) {
  const now = performance.now();
  (catalog || []).forEach((ch) => {
    const arr = spark[ch.key] || (spark[ch.key] = []);
    const v = values[ch.key];
    if (Number.isFinite(v)) arr.push([now, v]);
    const cut = now - SPARK_MS;
    while (arr.length && arr[0][0] < cut) arr.shift();
  });
}

function drawSparks() {
  $$("#params .prow").forEach((row) =>
    drawSpark(row.querySelector(".spark"), spark[row.dataset.key] || [], row.classList.contains("off"))
  );
}

function drawSpark(cv, hist, off) {
  if (!cv) return;
  const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
  if (w < 2 || h < 2) return;
  cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
  if (hist.length < 2) return;
  let mn = Infinity, mx = -Infinity;
  for (const p of hist) { if (p[1] < mn) mn = p[1]; if (p[1] > mx) mx = p[1]; }
  const span = (mx - mn) || 1, t1 = hist[hist.length - 1][0], pad = 2;
  const px = (t) => pad + ((t - (t1 - SPARK_MS)) / SPARK_MS) * (w - 2 * pad);
  const py = (v) => h - pad - ((v - mn) / span) * (h - 2 * pad);
  ctx.strokeStyle = cssVar("--info") || "#3aa0ff"; ctx.globalAlpha = off ? 0.5 : 1; ctx.lineWidth = 1.25;
  ctx.beginPath();
  hist.forEach((p, i) => { const X = px(p[0]), Y = py(p[1]); i ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); });
  ctx.stroke();
}

async function onSelectChange() {
  selectedKeys = new Set($$("#params .prow").filter((r) => $("input", r).checked).map((r) => r.dataset.key));
  $$("#params .prow").forEach((r) => r.classList.toggle("off", !$("input", r).checked));
  if (lastSnapshot) updateValues(lastSnapshot.values);
  applyParamMode();
  try {
    await api("/api/selected", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keys: [...selectedKeys] }),
    });
  } catch (e) {}
}

$("#selAll").addEventListener("click", () => { $$("#params input").forEach((i) => (i.checked = true)); onSelectChange(); });
$("#selNone").addEventListener("click", () => { $$("#params input").forEach((i) => (i.checked = false)); onSelectChange(); });

// ---------- read / pick mode ----------
// The checkbox list is the right shape for picking channels and the wrong one for
// reading them at a glance: 20+ rows with an 18 px number on the right. "read"
// keeps only the picked channels and lays them out as big tiles.
let paramMode = localStorage.getItem("paramMode") || "view";

function applyParamMode() {
  const box = $("#params");
  box.classList.toggle("params--view", paramMode === "view");
  $$("#paramMode button").forEach((b) => b.classList.toggle("on", b.dataset.pm === paramMode));
  $("#selAll").hidden = $("#selNone").hidden = paramMode === "view";
  const empty = paramMode === "view" && catalog.length && !selectedKeys.size;
  $("#paramsEmpty").hidden = !empty;
  drawSparks();
}
$$("#paramMode button").forEach((b) =>
  b.addEventListener("click", () => {
    paramMode = b.dataset.pm;
    try { localStorage.setItem("paramMode", paramMode); } catch (e) {}
    applyParamMode();
  }));

// ---------- appearance ----------
// Device-local, not board config: the same board is read in sunlight and at night.
function applyTheme(v) {
  if (v === "light" || v === "dark") document.documentElement.dataset.theme = v;
  else delete document.documentElement.dataset.theme;
  const sel = $("#themeSelect");
  if (sel) sel.value = v;
}
$("#themeSelect")?.addEventListener("change", (e) => {
  const v = e.target.value;
  try { localStorage.setItem("theme", v); } catch (err) {}
  applyTheme(v);
});

function logMeta(armed, file, records) {
  if (file) return `${t("rec.writing")}: ${file} · ${records || 0} ${t("rec.records")}`;
  if (armed) return t("rec.waiting");
  return t("rec.stopped");
}

// ---------- board link freshness ----------
// The push arrives every 0.2 s. When the Wi-Fi drops, connectWS() reconnects
// silently and the last snapshot just stays on screen: the pill still reads
// "connected" and the values are the last live numbers. On the bike that link
// breaks often, so a stale snapshot must look stale.
const STALE_MS = 2000;
let lastSnapAt = 0, staleOn = false;

function setStale(on) {
  if (staleOn === on) return;
  staleOn = on;
  document.body.classList.toggle("is-stale", on);
  if (on) {                                   // the next snapshot repaints the pill itself
    $("#statusPill").className = "pill pill--stale";
    $("#statusText").textContent = t("status.stale");
  }
  if (testingActive()) updateTestingButtons();
}
setInterval(() => {
  if (lastSnapAt && performance.now() - lastSnapAt > STALE_MS) setStale(true);
}, 500);

function applySnapshot(s) {
  lastSnapshot = s;
  lastSnapAt = performance.now();
  setStale(false);
  $("#statusPill").className = "pill pill--" + s.status;
  if (s.status === "connected") {                 // short pill: ecu:<model> hw:<hw> connected
    const m = (s.ecu_hw || "").match(/^(.*?)(HW\d+)$/);
    const model = m ? m[1] : (s.ecu_hw || s.ecu_id || "?");
    const hw = m ? m[2] : "";
    $("#statusText").textContent = `ecu:${model}${hw ? " hw:" + hw : ""} ${t("status.connected")}`;
  } else {
    $("#statusText").textContent = s.status_msg ? t(s.status_msg) : t("status." + s.status);
  }
  // full ECU identity -> green banner under the tabs; hidden when not connected
  const eb = $("#ecuBanner"), ef = s.ecu_fields || {};
  if (s.status === "connected" && Object.keys(ef).length) {
    eb.innerHTML = Object.entries(ef)
      .map(([k, v]) => `<span class="ecuf"><b>${esc(k)}:</b> ${esc(v)}</span>`).join("");
    eb.hidden = false;
  } else {
    eb.hidden = true;
  }
  $("#ecuId").textContent = s.ecu_hw || s.ecu_id || "—";
  const fwe = $("#fwEcuInfo");   // ECU identity shown in the Firmware Read block (-> .txt on read)
  if (fwe) fwe.textContent = (s.status === "connected" && s.ecu_desc) ? s.ecu_desc : t("fw.ecuNone");
  proposeReadName(ef.Drawing || "");
  // poll rate only meaningful when connected AND something is selected to poll
  const polling = s.status === "connected" && (s.selected || []).length > 0 && !s.scan_on;
  $("#pollHz").textContent = polling ? (s.poll_hz || 0).toFixed(1) + " " + t("unit.hz") : "—";
  $("#busBaud").textContent = s.bus_baud ? s.bus_baud + " " + t("unit.baud") : "—";

  const dT = $("#decToggle"), rT = $("#rawToggle");
  if (document.activeElement !== dT) dT.checked = s.logging_decoded;
  if (document.activeElement !== rT) rT.checked = s.logging_raw;
  $("#decMeta").textContent = logMeta(s.logging_decoded, s.log_decoded_file, s.log_decoded_records);
  $("#rawMeta").textContent = logMeta(s.logging_raw, s.log_raw_file, s.log_raw_records);

  const scanT = $("#scanToggle");
  if (scanT) {
    if (document.activeElement !== scanT) scanT.checked = !!s.scan_on;
    if (s.scan_on) {
      const total = s.scan_total || 0, pos = s.scan_pos || 0;
      const alive = `${t("scan.alive")} ${s.scan_alive || 0}`;
      if (total > 0 && pos < total) {           // first sweep in progress -> address = progress
        const pct = Math.round((pos / total) * 100);
        const hx = (n) => "0x" + n.toString(16).toUpperCase().padStart(2, "0");
        $("#scanMeta").textContent = `${t("scan.pass1")} ${pct}% (${hx(pos)}/${hx(total - 1)}) · ${alive}`;
      } else {
        const rem = s.scan_remaining;
        const clock = rem >= 0 ? ` · ${Math.floor(rem / 60)}:${String(rem % 60).padStart(2, "0")} ${t("scan.left")}` : "";
        $("#scanMeta").textContent = `${t("scan.running")} · ${alive}${clock}`;
      }
    } else {
      $("#scanMeta").textContent = t("scan.hint");
    }
    $("#scanDur").disabled = $("#scanFmt").disabled = !!s.scan_on;
    // idle scan block shows only its toggle + the two selects; everything else
    // (progress, markers, hint) appears once a sweep is actually running
    $("#scanBody").hidden = !s.scan_on;
    $(".scan-card").classList.toggle("is-running", !!s.scan_on);
    // scan and logging are mutually exclusive -> lock the log toggles while scanning
    dT.disabled = rT.disabled = !!s.scan_on;
    dT.closest(".rec-row").classList.toggle("is-locked", !!s.scan_on);
    rT.closest(".rec-row").classList.toggle("is-locked", !!s.scan_on);
    // say why the switch does not move instead of just greying it out
    if (s.scan_on) $("#decMeta").textContent = $("#rawMeta").textContent = t("err.busy_scan");
  }

  renderWifiStatus(s);

  if (!catalog.length && s.catalog.length) {
    catalog = s.catalog;
    selectedKeys = new Set(s.selected);
    renderParams();
  }
  updateValues(s.values);
  pushSpark(s.values);
  if (isLoggerActive()) drawSparks();
  if (testingActive()) updateTestingButtons();
}

function logToggleHandler(field) {
  return async (e) => {
    try {
      await api("/api/logging", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [field]: e.target.checked }),
      });
    } catch (err) { toastErr(err); }
  };
}
$("#decToggle").addEventListener("change", logToggleHandler("decoded"));
$("#rawToggle").addEventListener("change", logToggleHandler("raw"));

$("#scanToggle").addEventListener("change", async (e) => {
  // full parameter scan: sweep 0x00..0xFF in both framings
  const fmt = $("#scanFmt").value === "both" ? "both" : 2;
  const body = e.target.checked
    ? { on: true, start: 0, end: 255, fmt, duration: parseInt($("#scanDur").value, 10) || 0 }
    : { on: false };
  try {
    await api("/api/kline/scan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (err) { toastErr(err); }
});

// ---------- testing (diagnostics: read / clear fault codes) ----------
const testingActive = () => $("#tab-testing").classList.contains("is-active");
const TPS_FULL_DEG = 90;    // fixed scale: 0x34 reads degrees, closed sits at ~1.8

function updateTpsMeter() {
  const meter = $(".tps-meter");
  if (!meter) return;
  const v = lastSnapshot && lastSnapshot.values ? lastSnapshot.values.throttle : null;
  const live = Number.isFinite(v);
  const pct = live ? Math.max(0, Math.min(100, (v / TPS_FULL_DEG) * 100)) : 0;
  $("#tpsFill").style.width = pct.toFixed(1) + "%";
  $("#tpsVal").textContent = live ? `${v.toFixed(1)}° · ${Math.round(pct)} %` : "—";
  meter.classList.toggle("live", live);
  meter.title = live ? `0x34 Throttle · 0–${TPS_FULL_DEG}°` : t("test.tpsNeed");
}

function updateTestingButtons() {
  const s = lastSnapshot || {};
  const ok = s.status === "connected" && !s.scan_on && !staleOn;
  // one thing on the bus at a time: while an actuator is energized every other
  // test is locked out (GuzziDiag does the same on its test screen)
  const busy = s.act_lid !== null && s.act_lid !== undefined;
  ["#dtcReadBtn", "#dtcClearBtn", "#tpsResetBtn", "#adaptResetBtn"].forEach((id) => {
    const b = $(id); if (b) b.disabled = !ok || busy;
  });
  $$(".act-btn").forEach((b) => (b.disabled = !ok || busy));
  $("#testConnHint").classList.toggle("ok", ok);
  const stop = $("#actStopBtn");
  if (stop) { stop.hidden = !busy; stop.disabled = !ok; }
  const run = $("#actRunning");
  if (run) {
    run.hidden = !busy;
    if (busy) {
      const left = Math.max(0, Math.ceil((s.act_until || 0) - Date.now() / 1000));
      run.textContent = `${s.act_key || s.act_lid} — ${left} ${t("unit.sec")}`;
    }
  }
  updateTpsMeter();
  const badge = $("#testModeBadge");
  if (badge) {
    badge.textContent = s.test_mode ? t("test.modeOn") : t("test.modeOff");
    badge.classList.toggle("ok", !!s.test_mode);
    badge.title = s.test_mode_detail || "";
  }
}
let actLoaded = false;
async function loadActuators() {
  if (actLoaded) return;
  let d;
  try { d = await api("/api/actuators"); } catch (e) { return; }
  actLoaded = true;
  statusMaps = d.status_maps || {};
  const box = $("#actList"); box.innerHTML = "";
  const all = d.actuators || [];
  const pulse = $("#actPulse");
  if (pulse && d.pulse_ms) {
    pulse.value = (d.pulse_ms / 1000).toString();
    pulse.min = ((d.min_ms || 500) / 1000).toString();
    pulse.max = ((d.max_ms || 30000) / 1000).toString();
  }
  [["test.actBike", all.filter((a) => a.bike)],
   ["test.actOther", all.filter((a) => !a.bike)]].forEach(([lbl, list]) => {
    if (!list.length) return;
    const h = document.createElement("div");
    h.className = "act-group-lbl";
    h.textContent = t(lbl);
    box.appendChild(h);
    const grid = document.createElement("div");
    grid.className = "act-grid";
    list.forEach((a) => {
      const b = document.createElement("button");
      b.className = "mini act-btn" + (a.bike ? "" : " act-other");
      // "*" = in the IAW set but possibly absent on this bike (see test.actFootnote)
      b.textContent = a.name + (a.bike ? "" : "*");
      b.addEventListener("click", () => runActuator(a));
      grid.appendChild(b);
    });
    box.appendChild(grid);
  });
  const sm = $("#statusMaps"); sm.innerHTML = "";
  Object.entries(d.status_maps || {}).forEach(([name, m]) => {
    const items = Object.entries(m.values)
      .map(([k, v]) => `<span class="sref-item"><b>${k}</b> ${v}</span>`).join("");
    sm.insertAdjacentHTML("beforeend",
      `<div class="sref"><div class="sref-name">${name} <em>(${m.type})</em></div>${items}</div>`);
  });
  updateTestingButtons();
}
async function runActuator(a) {
  const secs = parseFloat($("#actPulse").value) || 5;
  const ask = t("test.confirmAct").replace("%s", a.name).replace("%d", secs);
  if (!(await confirmDialog(ask, { danger: true, okLabel: a.name }))) return;
  if (await engineRunningBlocked()) return;
  await runTest("#actResult", "actuator/" + a.localid, "test.actDone");
}
async function savePulse() {
  const el = $("#actPulse");
  let secs = parseFloat(el.value);
  if (!(secs >= 0.5)) secs = 0.5;
  if (secs > 30) secs = 30;
  el.value = secs.toString();
  try {
    const r = await api("/api/testing/settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pulse_ms: Math.round(secs * 1000) }),
    });
    if (r && r.pulse_ms) el.value = (r.pulse_ms / 1000).toString();
  } catch (e) { /* keep the typed value; the server clamps on use anyway */ }
}
let profLoaded = false;
async function loadProfiles() {
  if (profLoaded) return;
  let d;
  try { d = await api("/api/profiles"); } catch (e) { return; }
  profLoaded = true;
  const sel = $("#profSelect"), models = d.models || {}, ecus = d.ecus || {};
  sel.innerHTML = "";
  Object.keys(models).forEach((name) => {
    const o = document.createElement("option");
    o.value = name; o.textContent = name;
    sel.appendChild(o);
  });
  const render = () => {
    const m = models[sel.value] || {};
    const e = ecus[m.ecu] || {};
    const rows = [[t("test.profEcu"), m.ecu + (m.protocol ? " · " + m.protocol : "")]];
    if (e.name) rows.push([t("test.profName"), e.name]);
    if (e.rli_range) rows.push(["rli", e.rli_range.join("–")]);
    if (e.dtc) rows.push(["DTC", `read ${e.dtc.read} · clear ${e.dtc.clear}`]);
    Object.entries(e.resets || {}).forEach(([k, v]) => rows.push([k, v]));
    if (e.actuators) rows.push([t("test.actTitle"),
      Object.entries(e.actuators).map(([k, v]) => `${k} ${v}`).join(", ")]);
    if (e.co_trim) rows.push(["CO trim", `${e.co_trim.test} / ${e.co_trim.store} — ${e.co_trim.status}`]);
    if (e.note) rows.push(["note", e.note]);
    if (e.source) rows.push(["source", e.source]);
    $("#profInfo").innerHTML = rows.map(([k, v]) =>
      `<div class="prof-row"><span class="prof-k">${k}</span><span class="prof-v">${v}</span></div>`).join("");
  };
  // preselect a 5AM bike so the default view is the ECU this logger targets
  const first5am = Object.keys(models).find((n) => models[n].ecu === "5AM");
  if (first5am) sel.value = first5am;
  sel.addEventListener("change", render);
  render();
}

function testingEnter() {
  $("#dtcResult").innerHTML = "";
  setDtcCopy("");
  loadActuators();
  loadProfiles();
  updateTestingButtons();
}
// What Copy puts on the clipboard. Kept as a plain-text twin of the result box:
// scraping the rendered HTML back out would lose the status byte (it only lives
// in a title=) and glue the columns together without separators.
let dtcCopyText = "";
function setDtcCopy(text) {
  dtcCopyText = text || "";
  const btn = $("#dtcCopyBtn");
  // deliberately not part of updateTestingButtons(): the result stays worth
  // copying after the ECU link drops, which is exactly when you want to paste it
  if (btn) btn.hidden = !dtcCopyText;
}
const dtcCopyHead = () => `${t("test.faultTitle")} · ${new Date().toLocaleString()}`;

function renderDtc(cmd, res) {
  const box = $("#dtcResult");
  if (!res || !res.ok) {
    const msg = res && res.error ? (t(res.error) || res.error) : t("banner.error");
    const detail = res && res.detail ? " (" + res.detail + ")" : "";
    box.innerHTML = `<p class="dtc-err">${msg}${detail}</p>`;
    setDtcCopy(`${dtcCopyHead()}\n${msg}${detail}`);
    return;
  }
  if (cmd === "clear_dtc") {
    box.innerHTML = `<p class="dtc-ok">${t("test.cleared")}</p>`;
    setDtcCopy(`${dtcCopyHead()}\n${t("test.cleared")}`);
    return;
  }
  if (!res.count) {
    box.innerHTML = `<p class="dtc-ok">${t("test.none")}</p>`;
    setDtcCopy(`${dtcCopyHead()}\n${t("test.none")}`);
    return;
  }
  const lines = [dtcCopyHead(), `${res.count} ${t("test.found")}`];
  const rows = res.dtcs.map((d) => {
    // status byte as both PC tools read it: bit 0x20 = stored, low nibble = kind
    const state = d.stored ? t("dtc.stored") : t("dtc.current");
    const kind = d.kind ? ` · ${t("dtc.kind")} ${d.kind}` : "";
    const warn = d.warn ? " · ⚠" : "";
    const hex = `0x${(d.status || 0).toString(16).padStart(2, "0")}`;
    lines.push(`${d.code}${d.desc ? " · " + d.desc : ""} · ${state}${kind}${warn} · ${hex}`);
    return `<div class="dtc-row"><span class="dtc-code">${d.code}</span>` +
      (d.desc ? `<span class="dtc-desc">${d.desc}</span>` : "") +
      `<span class="dtc-st" title="${hex} (${d.bits || ""})">` +
      `${state}${kind}${warn}</span></div>`;
  }).join("");
  box.innerHTML = `<p class="dtc-ok">${res.count} ${t("test.found")}</p>${rows}` +
    `<p class="dtc-raw">${res.raw || ""}</p>`;
  if (res.raw) lines.push(res.raw);
  setDtcCopy(lines.join("\n"));
}
async function runDtc(cmd) {
  $("#dtcResult").innerHTML = `<p class="hint">${t("test.running")}</p>`;
  setDtcCopy("");
  let res;
  try {
    res = await api("/api/testing/" + cmd, { method: "POST" });
  } catch (err) {
    const msg = `${t("banner.error")} ${err.message}`;
    $("#dtcResult").innerHTML = `<p class="dtc-err">${msg}</p>`;
    setDtcCopy(`${dtcCopyHead()}\n${msg}`);
    return;
  }
  renderDtc(cmd, res);
}
$("#dtcReadBtn").addEventListener("click", (e) => withBusy(e.currentTarget, () => runDtc("read_dtc")));
$("#dtcClearBtn").addEventListener("click", async (e) => {
  if (!(await confirmDialog(t("test.confirmClear"), { danger: true, okLabel: t("test.clear") }))) return;
  await withBusy(e.currentTarget, () => runDtc("clear_dtc"));
});
$("#dtcCopyBtn").addEventListener("click", async () => {
  const ok = await copyToClipboard(dtcCopyText);
  toast(t(ok ? "test.copied" : "test.copyFail"), ok ? "ok" : "err");
});

// generic one-shot Testing command with a result box
async function runTest(boxSel, cmd, okKey) {
  const box = $(boxSel);
  box.innerHTML = `<p class="hint">${t("test.running")}</p>`;
  let res;
  try { res = await api("/api/testing/" + cmd, { method: "POST" }); }
  catch (err) { box.innerHTML = `<p class="dtc-err">${t("banner.error")} ${err.message}</p>`; return; }
  if (!res.ok) {
    const msg = res.error ? (t(res.error) || res.error) : t("banner.error");
    box.innerHTML = `<p class="dtc-err">${msg}${res.detail ? " (" + res.detail + ")" : ""}</p>`;
  } else {
    box.innerHTML = `<p class="dtc-ok">${t(okKey)}</p><p class="dtc-raw">${res.raw || ""}</p>`;
  }
}
// soft guard: warn if the engine seems to be running (rpm live)
async function engineRunningBlocked() {
  const rpm = (lastSnapshot && lastSnapshot.values && lastSnapshot.values.rpm) || 0;
  if (rpm <= 200) return false;
  return !(await confirmDialog(t("test.engineRunning"), { danger: true }));
}
$("#tpsResetBtn").addEventListener("click", async (e) => {
  if (!(await confirmDialog(t("test.confirmTps"), { danger: true, okLabel: t("test.tps") }))) return;
  if (await engineRunningBlocked()) return;
  await withBusy(e.currentTarget, () => runTest("#adaptResult", "reset_tps", "test.tpsDone"));
});
$("#adaptResetBtn").addEventListener("click", async (e) => {
  if (!(await confirmDialog(t("test.confirmAdapt"), { danger: true, okLabel: t("test.adapt") }))) return;
  if (await engineRunningBlocked()) return;
  await withBusy(e.currentTarget, () => runTest("#adaptResult", "reset_adaptation", "test.adaptDone"));
});

// ---------- scan event markers (status-channel identification) ----------
let markTimer = null;

// Solo testing: you cannot flip a switch and press a button at the same instant.
// A delay lets the marker land exactly when you are at the switch — press, walk,
// flip on zero.
function sendMark(label) {
  const secs = parseInt($("#markDelay").value, 10) || 0;
  clearInterval(markTimer);
  if (!secs) return postMark(label);
  const el = $("#markCount");
  let left = secs;
  el.hidden = false;
  el.textContent = left + " s";
  markTimer = setInterval(() => {
    left -= 1;
    if (left > 0) { el.textContent = left + " s"; return; }
    clearInterval(markTimer);
    el.hidden = true;
    postMark(label);
  }, 1000);
}

async function postMark(label) {
  const box = $("#markBar");
  try {
    const r = await api("/api/kline/scan/mark", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label }),
    });
    box.classList.toggle("marked", !!r.ok);
    setTimeout(() => box.classList.remove("marked"), 600);
  } catch (e) { /* scan not running -> server already answered err.no_scan */ }
}
$$(".mark-btn").forEach((b) => b.addEventListener("click", () => sendMark(b.dataset.mark)));
$("#markBtn").addEventListener("click", () => {
  const el = $("#markText");
  sendMark(el.value.trim() || "mark");
  el.value = "";
});

$("#actPulse").addEventListener("change", savePulse);
$("#actStopBtn").addEventListener("click", () => runTest("#actResult", "actuator/stop", "test.actStopped"));

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (ev) => { try { applySnapshot(JSON.parse(ev.data)); } catch (e) {} };
  ws.onclose = () => setTimeout(connectWS, 1500);
  ws.onerror = () => ws.close();
}

// ---------- config ----------
async function loadConfig() {
  let cfg;
  try { cfg = await api("/api/config"); } catch (e) { return; }
  $$("#cfgForm [name]").forEach((el) => {
    const v = getNested(cfg, el.name);
    if (el.type === "checkbox") el.checked = !!v;
    else if (v != null) el.value = v;
  });
  $("#modeToggle").checked = getNested(cfg, "wifi.mode") === "client";
  $("#dhcpClientToggle").checked = getNested(cfg, "wifi.client.ipv4") !== "static";
  $("#clientMask").value = prefixToMask(getNested(cfg, "wifi.client.prefix") || 24);
  applyWifiMode();
  $("#channelSelect").value = cfg.wifi.auto_channel ? "auto" : String(cfg.wifi.channel);
  cfgLoaded = cfg;
  markNetDirty();
  loadWifiChart();
}

// ---------- Wi-Fi mode (access point <-> client) ----------
// The visible controls are a plain toggle (AP on the left, Client on the right)
// and a DHCP-client toggle; both feed hidden fields so the config payload keeps
// its "wifi.mode" / "wifi.client.ipv4" shape.
const wifiMode = () => ($("#modeToggle").checked ? "client" : "ap");
const clientIpv4 = () => ($("#dhcpClientToggle").checked ? "dhcp" : "static");

function maskToPrefix(mask) {
  const parts = String(mask).trim().split(".");
  if (parts.length !== 4) return null;
  let bits = "";
  for (const p of parts) {
    const n = Number(p);
    if (!Number.isInteger(n) || n < 0 || n > 255) return null;
    bits += n.toString(2).padStart(8, "0");
  }
  return /^1*0*$/.test(bits) ? bits.replace(/0/g, "").length : null;   // contiguous only
}
function prefixToMask(prefix) {
  const n = Math.max(0, Math.min(32, Number(prefix) || 0));
  const bits = "1".repeat(n).padEnd(32, "0");
  return [0, 8, 16, 24].map((i) => parseInt(bits.slice(i, i + 8), 2)).join(".");
}

function applyWifiMode() {
  const client = wifiMode() === "client";
  $('#cfgForm [name="wifi.mode"]').value = client ? "client" : "ap";
  $('#cfgForm [name="wifi.client.ipv4"]').value = clientIpv4();
  $(".mode-ap").hidden = client;
  $(".mode-client").hidden = !client;
  $(".net-ap").hidden = client;
  $(".net-client").hidden = !client;
  $("#modeLblAp").classList.toggle("is-on", !client);
  $("#modeLblClient").classList.toggle("is-on", client);
  // DHCP client on -> the manual address fields stay visible but greyed out
  const manual = clientIpv4() === "static";
  const box = $(".client-static");
  box.classList.toggle("is-off", !manual);
  $$("input", box).forEach((el) => (el.disabled = !manual));
  const hint = $("#wifiModeHint");
  hint.dataset.i18n = client ? "cfg.modeClientHint" : "cfg.modeApHint";
  hint.textContent = t(hint.dataset.i18n);
}

function renderWifiStatus(s) {
  const box = $("#wifiStatus");
  if (!box) return;
  const link = s.wifi_link || {};
  if (s.wifi_mode === "client") {
    const where = link.associated
      ? `${link.ssid || "?"}${link.ip ? " · " + link.ip : ""}${link.signal != null ? " · " + link.signal + " dBm" : ""}`
      : t("cfg.stNotJoined");
    box.textContent = `${t("cfg.modeClient")}: ${where}`;
    box.classList.toggle("ok", !!link.associated);
  } else {
    box.textContent = `${t("cfg.modeAp")}: ${t("cfg.stServing")}`;
    box.classList.add("ok");
  }
}

async function scanNetworks() {
  const btn = $("#netScanBtn"), meta = $("#netScanMeta"), list = $("#netList");
  if (!(await confirmDialog(t("cfg.scanWarn"), { danger: true, okLabel: t("cfg.scanNets") }))) return;
  btn.disabled = true;
  meta.textContent = t("cfg.scanRunning");
  let d;
  try { d = await api("/api/wifi/networks", { method: "POST" }); }
  catch (e) { meta.textContent = cfgErrText(e); btn.disabled = false; return; }
  btn.disabled = false;
  list.innerHTML = "";
  if (!d.ok || !(d.networks || []).length) {
    meta.textContent = d.error === "err.no_iface" ? t("err.no_iface") : t("cfg.scanNone");
    return;
  }
  meta.textContent = `${d.networks.length} ${t("cfg.scanFound")}`;
  d.networks.forEach((n) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "mini net-item";
    b.textContent = `${n.ssid} · ${n.signal} dBm${n.secured ? " 🔒" : ""}${n.channel ? " · ch " + n.channel : " · 5 GHz"}`;
    b.addEventListener("click", () => {
      $('#cfgForm [name="wifi.client.ssid"]').value = n.ssid;
      markNetDirty();
      $$(".net-item").forEach((x) => x.classList.remove("is-sel"));
      b.classList.add("is-sel");
    });
    list.appendChild(b);
  });
}

$("#modeToggle").addEventListener("change", () => { applyWifiMode(); markNetDirty(); });
$("#dhcpClientToggle").addEventListener("change", () => { applyWifiMode(); markNetDirty(); });
$("#clientMask").addEventListener("change", (e) => {
  const pfx = maskToPrefix(e.target.value);
  const hidden = $('#cfgForm [name="wifi.client.prefix"]');
  if (pfx === null) { e.target.value = prefixToMask(hidden.value); return; }   // reject junk
  hidden.value = pfx;
  markNetDirty();
});

$("#netScanBtn").addEventListener("click", scanNetworks);

async function loadWifiChart() {
  const box = $("#wifiChart");
  const meta = $("#wifiScanMeta");
  let d;
  try { d = await api("/api/wifi/scan"); } catch (e) { return; }
  box.innerHTML = "";
  if (!d.survey || !d.survey.length) {
    box.innerHTML = `<span class="hint">${t("chart.nodata")}</span>`;
    meta.textContent = "";
    return;
  }
  meta.textContent = d.chosen ? t("chart.chosen") + " " + d.chosen : "";
  const max = Math.max(1e-9, ...d.survey.map((s) => s.load));
  d.survey.forEach((s) => {
    const bar = document.createElement("div");
    bar.className = "wbar" + (s.channel === d.chosen ? " chosen" : "");
    bar.title = `ch ${s.channel}: ${s.count} AP`;
    const h = Math.round(4 + 52 * (s.load / max));
    bar.innerHTML = `<div class="wbar-track"><div class="wbar-fill" style="height:${h}px"></div></div><div class="wbar-lbl">${s.channel}</div>`;
    box.appendChild(bar);
  });
}

// Network fields restart the AP when saved, so they are never auto-saved: editing
// SSID/IP field by field would drop the very connection carrying the edits.
const NET_FIELD = (name) => /^(wifi|network|dhcp)\./.test(name) || name === "hostname";

function fieldValue(el) {
  if (el.type === "checkbox") return el.checked;
  if (el.type === "number") return el.value === "" ? null : Number(el.value);
  return el.value;
}

function buildConfigPayload(only) {
  const payload = {};
  $$("#cfgForm [name]").forEach((el) => {
    if (only === "net" && !NET_FIELD(el.name)) return;
    if (only === "plain" && NET_FIELD(el.name)) return;
    const v = fieldValue(el);
    if (v !== null && v !== undefined) setNested(payload, el.name, v);
  });
  if (only !== "plain") {
    const cv = $("#channelSelect").value;
    if (cv === "auto") setNested(payload, "wifi.auto_channel", true);
    else { setNested(payload, "wifi.auto_channel", false); setNested(payload, "wifi.channel", Number(cv)); }
  }
  return payload;
}

// a fetch that never reached the board reports a bare "Load failed"/"Failed to
// fetch"; say what that actually means (service restarting, AP bounced, cable out)
function cfgErrText(err) {
  const m = String(err && err.message);
  return /load failed|failed to fetch|networkerror/i.test(m)
    ? t("cfg.unreachable") : t("banner.error") + " " + m;
}

function showCfgBanner(cls, html, autoHideMs) {
  const banner = $("#cfgBanner");
  banner.className = "banner " + cls;
  banner.innerHTML = html;
  banner.hidden = false;
  clearTimeout(showCfgBanner._t);
  if (autoHideMs) showCfgBanner._t = setTimeout(() => (banner.hidden = true), autoHideMs);
}

// mark network fields that differ from what the board has, so it is obvious they
// still need the Save button
let cfgLoaded = null;
function markNetDirty() {
  let n = 0;
  $$("#cfgForm [name]").forEach((el) => {
    if (!NET_FIELD(el.name) || !cfgLoaded) return;
    const was = getNested(cfgLoaded, el.name);
    const now = fieldValue(el);
    const changed = String(was ?? "") !== String(now ?? "");
    el.closest("label")?.classList.toggle("is-dirty", changed);
    if (changed) n++;
  });
  $("#cfgNetDirty").hidden = !n;
  const bar = $("#cfgDirtyBar");
  if (bar) {
    bar.hidden = !n;
    if (n) $("#cfgDirtyText").textContent = t("cfg.dirtyN").replace("%d", n);
  }
}

$("#cfgApply")?.addEventListener("click", () => $("#cfgForm").requestSubmit());
$("#cfgRevert")?.addEventListener("click", () => loadConfig());

async function autoSaveField(el) {
  try {
    const rep = await api("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildConfigPayload("plain")),
    });
    cfgLoaded = await api("/api/config");
    const what = (rep.applied || []).join(", ");
    showCfgBanner("ok", `<b>${t("cfg.saved")}</b>${what ? " — " + what : ""}`, 3000);
  } catch (err) {
    showCfgBanner("err", cfgErrText(err));
  }
}

$("#cfgForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const banner = $("#cfgBanner");
  try {
    const rep = await api("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildConfigPayload()),
    });
    let cls = "ok", lines = [`<b>${t("cfg.saved")}</b>`];
    if (rep.applied?.length) lines.push(`${t("apply.applied")}<ul>` + rep.applied.map((a) => `<li>${a}</li>`).join("") + "</ul>");
    if (rep.iface_missing) lines.push(t("cfg.noIface"));
    if (rep.reconnect_required) { cls = "warn"; lines.push(`${t("apply.reconnect")} <b>http://${rep.ap_ip}</b>`); }
    if (rep.reboot_recommended) { cls = "warn"; lines.push(t("apply.reboot")); }
    banner.className = "banner " + cls;
    banner.innerHTML = lines.join("");
    banner.hidden = false;
    cfgLoaded = await api("/api/config");
    markNetDirty();
  } catch (err) {
    banner.className = "banner err";
    banner.textContent = cfgErrText(err);
    banner.hidden = false;
  }
  banner.scrollIntoView({ behavior: "smooth", block: "nearest" });
});

// Everything that is not a network field saves itself on change (green banner);
// network fields wait for Save, because applying them restarts the AP.
$("#cfgForm").addEventListener("change", (e) => {
  const el = e.target;
  if (!el.name) { markNetDirty(); return; }        // e.g. #channelSelect
  if (NET_FIELD(el.name)) { markNetDirty(); return; }
  autoSaveField(el);
});

// locale also switches the UI instantly; the change listener above persists it
$("#localeSelect").addEventListener("change", (e) => applyLocale(e.target.value));

// ---------- firmware operation logs (Config -> System) ----------
// Deliberately not in the Logs tab: a flash log is a diagnostics artefact, and
// mixing it into the ride logs made both lists harder to scan.
async function loadFwLogs() {
  const box = $("#fwLogsList");
  if (!box) return;
  let d;
  try { d = await api("/api/firmware/logs"); } catch (e) { return; }
  if (!d.files.length) {
    box.innerHTML = `<p class="hint" data-i18n="cfg.fwLogsEmpty">${esc(t("cfg.fwLogsEmpty"))}</p>`;
    return;
  }
  box.innerHTML = d.files.map((f) => {
    const cur = f.name === d.current ? ' <span class="badge dec">now</span>' : "";
    return `<div class="logrow">` +
      `<a href="/api/firmware/log.txt?file=${encodeURIComponent(f.name)}" target="_blank" rel="noopener">${esc(f.name)}</a>${cur}` +
      `<span class="lmeta">${fmtDate(f.mtime)} · ${fmtSize(f.size)}</span>` +
      `<a class="mini" href="/api/logs/${encodeURIComponent(f.name)}" download>${esc(t("logs.download"))}</a>` +
      `</div>`;
  }).join("");
}

$("#fwLogsRefresh")?.addEventListener("click", loadFwLogs);

// ---------- board clock ----------
// No internet on the bike, so timesyncd may be running and still be wrong. The
// browser is the only trustworthy clock around — offer to push it.
const fmtTime = (d) => d.toLocaleString(undefined, { dateStyle: "short", timeStyle: "medium" });

function showBrowserTime() {
  $("#timeBrowser").textContent = fmtTime(new Date());
}

async function loadTime() {
  showBrowserTime();
  let st;
  try { st = await api("/api/system/time"); } catch (e) { return; }
  $("#timeBoard").textContent = fmtTime(new Date(st.epoch * 1000));
  const drift = Math.round(Math.abs(st.epoch * 1000 - Date.now()) / 1000);
  const bits = [st.tz || "—", `${t("cfg.timeDrift")} ${drift} ${t("unit.sec")}`];
  // NTP being "active" means nothing off-grid — it just may correct us later
  if (st.ntp_active) bits.push(t("cfg.timeNtp"));
  bits.push(!st.auto_enabled ? t("cfg.autoTimeOff")
            : st.auto_done ? t("cfg.autoTimeDone") : t("cfg.autoTimeWait"));
  $("#timeMeta").textContent = bits.join(" · ");
}

$("#timeSyncBtn").addEventListener("click", async () => {
  const meta = $("#timeMeta");
  let res;
  try {
    res = await api("/api/system/time", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        epoch: Date.now() / 1000,
        tz: Intl.DateTimeFormat().resolvedOptions().timeZone || "",
      }),
    });
  } catch (e) { meta.textContent = cfgErrText(e); return; }
  if (!res.ok) {
    meta.textContent = t(res.error) || res.error;
  } else {
    showCfgBanner("ok", `<b>${t("cfg.timeDone")}</b> ${(res.applied || []).join(" · ")}`, 3000);
  }
  loadTime();
});

$("#btnReboot").addEventListener("click", async () => {
  if (!(await confirmDialog(t("confirm.reboot"), { danger: true, okLabel: t("cfg.reboot") }))) return;
  try { await api("/api/system/reboot", { method: "POST" }); toast(t("power.rebooting"), "warn", 15000); }
  catch (e) { toastErr(e); }
});
$("#btnShutdown").addEventListener("click", async () => {
  if (!(await confirmDialog(t("confirm.shutdown"), { danger: true, okLabel: t("cfg.shutdown") }))) return;
  try { await api("/api/system/shutdown", { method: "POST" }); toast(t("power.shutting"), "warn", 15000); }
  catch (e) { toastErr(e); }
});

// ---------- logs ----------
let logsData = [];
let logFilter = "all";
let logSortKey = "mtime";
let logSortDir = -1;

async function loadLogs() {
  let data;
  try { data = await api("/api/logs"); } catch (e) { return; }
  logsData = data.files || [];
  try { $("#logZip").checked = !!(await api("/api/config")).logging.zip_after; } catch (e) {}
  setDiskFree($("#logsFree"), data);
  logSortArrows();
  renderLogs();
}

// day key YYYY-MM-DD from mtime, falling back to the YYYYMMDD in the filename
function logDay(f) {
  if (f.mtime) {
    const d = new Date(f.mtime * 1000);
    if (d.getFullYear() > 1970) {
      const p = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    }
  }
  const m = String(f.name).match(/(\d{4})(\d{2})(\d{2})/);
  return m ? `${m[1]}-${m[2]}-${m[3]}` : "—";
}
function logsVisible() {
  return logFilter === "all" ? logsData : logsData.filter((f) => f.kind === logFilter);
}
function updateDayChecks() {
  $$("#logsList .logday").forEach((chk) => {
    const wrap = chk.closest(".log-group").nextElementSibling;
    const boxes = [...wrap.querySelectorAll(".logsel")];
    const on = boxes.filter((b) => b.checked).length;
    chk.checked = on > 0 && on === boxes.length;
    chk.indeterminate = on > 0 && on < boxes.length;
  });
}

function renderLogs() {
  const list = $("#logsList");
  const checked = new Set([...list.querySelectorAll(".logsel:checked")].map((c) => c.value));
  let rows = logsVisible();
  const k = logSortKey, dir = logSortDir;
  rows = rows.slice().sort((a, b) => {
    let x = a[k], y = b[k];
    if (k === "name") { x = String(x).toLowerCase(); y = String(y).toLowerCase(); }
    return x < y ? -dir : x > y ? dir : 0;
  });
  list.innerHTML = "";
  if (!rows.length) { list.innerHTML = `<p class="hint">${t("logs.empty")}</p>`; return; }

  const groups = new Map();                         // day -> rows (sorted order kept)
  rows.forEach((f) => { const d = logDay(f); (groups.get(d) || groups.set(d, []).get(d)).push(f); });
  const days = [...groups.keys()].sort().reverse(); // latest day first

  days.forEach((day, gi) => {
    const items = groups.get(day);
    const anyChecked = items.some((f) => checked.has(f.name));
    const total = items.reduce((s, f) => s + (f.size || 0), 0);

    const hdr = document.createElement("div");
    hdr.className = "log-group";
    const wrap = document.createElement("div");
    wrap.className = "log-group-wrap";
    wrap.hidden = gi !== 0 && !anyChecked;          // latest open; others only if a row is checked

    const dayChk = document.createElement("input");
    dayChk.type = "checkbox"; dayChk.className = "logday"; dayChk.dataset.date = day;
    dayChk.addEventListener("change", () => {
      wrap.querySelectorAll(".logsel").forEach((c) => (c.checked = dayChk.checked));
      updateDayChecks();
    });
    const btn = document.createElement("button");
    btn.type = "button"; btn.className = "log-group-btn";
    const relabel = () =>
      (btn.textContent = `${wrap.hidden ? "▸" : "▾"} ${day} · ${items.length} ${t("logs.groupCount")} · ${fmtSize(total)}`);
    btn.addEventListener("click", () => { wrap.hidden = !wrap.hidden; relabel(); });
    relabel();
    hdr.appendChild(dayChk); hdr.appendChild(btn);

    items.forEach((f) => {
      const row = document.createElement("div");
      row.className = "fwrow";
      // three kinds now: decoded CSV, raw frames, and the board diagnostics log
      const bk = f.kind === "raw" ? "raw" : f.kind === "diag" ? "diag" : f.kind === "fw" ? "fw" : "dec";
      const badge = `<span class="badge ${bk === "raw" ? "raw" : "dec"}">${bk}</span>`;
      const zip = f.zip ? '<span class="badge zip">zip</span>' : "";
      const date = fmtDate(f.mtime);
      row.innerHTML =
        `<input type="checkbox" class="logsel" value="${esc(f.name)}"${checked.has(f.name) ? " checked" : ""} />` +
        `<span class="fw-file__name" title="${esc(f.name)}">${badge}${zip}${esc(f.name)}</span>` +
        `<span class="col-date">${date || ""}</span>` +
        `<span class="col-size">${fmtSize(f.size)}</span>`;
      $("input", row).addEventListener("change", updateDayChecks);
      wrap.appendChild(row);
    });
    list.appendChild(hdr); list.appendChild(wrap);
  });
  updateDayChecks();
}

const logChecked = () => [...document.querySelectorAll("#logsList .logsel:checked")].map((c) => c.value);

function logSortArrows() {
  $$("#logSort button").forEach((el) => {
    const arr = el.querySelector(".arrow");
    if (el.dataset.s === logSortKey) { el.classList.add("on"); if (arr) arr.textContent = logSortDir > 0 ? "▲" : "▼"; }
    else { el.classList.remove("on"); if (arr) arr.textContent = ""; }
  });
}

$("#logsRefresh").addEventListener("click", loadLogs);
$("#logFilter").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  logFilter = b.dataset.f;
  [...$("#logFilter").children].forEach((x) => x.classList.toggle("on", x === b));
  renderLogs();
});
$("#logSort").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  const k = b.dataset.s;
  if (logSortKey === k) logSortDir = -logSortDir;
  else { logSortKey = k; logSortDir = k === "name" ? 1 : -1; }
  logSortArrows();
  renderLogs();
});
$("#logZip").addEventListener("change", async (e) => {
  try {
    await api("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ logging: { zip_after: e.target.checked } }) });
  } catch (err) { toastErr(err); }
});
function saveBlobAs(blobOrUrl, filename) {
  const a = document.createElement("a");
  a.href = blobOrUrl; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
}
$("#logsDownloadBtn").addEventListener("click", async () => {
  const sel = logChecked();
  if (!sel.length) return;
  // group the selection by day; a day is "full" when all its VISIBLE files are picked
  const visible = logsVisible();
  const byDay = new Map();
  visible.forEach((f) => { const d = logDay(f); (byDay.get(d) || byDay.set(d, []).get(d)).push(f.name); });
  const selByDay = new Map();
  sel.forEach((n) => {
    const f = logsData.find((x) => x.name === n); const d = f ? logDay(f) : "—";
    (selByDay.get(d) || selByDay.set(d, []).get(d)).push(n);
  });
  const fullDays = []; let partial = false;
  for (const [d, names] of selByDay) {
    if (names.length === (byDay.get(d) || []).length) fullDays.push(d); else partial = true;
  }
  const ymd = (d) => d.replace(/-/g, "");
  const today = ymd(logDay({ mtime: Date.now() / 1000 }));
  const zipname = (fullDays.length && !partial)
    ? "k-line-" + fullDays.sort().map(ymd).join("-") + ".log.zip"
    : "k-line-" + today + ".log.zip";

  if (sel.length === 1 && partial) {                       // lone loose file -> direct download
    saveBlobAs("/api/logs/" + encodeURIComponent(sel[0]), sel[0]);
    return;
  }
  try {                                                     // otherwise bundle into one zip
    const r = await fetch("/api/logs/download", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names: sel, zipname }),
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const url = URL.createObjectURL(await r.blob());
    saveBlobAs(url, zipname);
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  } catch (err) { toastErr(err); }
});
$("#logsDeleteBtn").addEventListener("click", async () => {
  const names = logChecked(); if (!names.length) return;
  if (!(await confirmDialog(t("fw.confirmDelete"), { danger: true, okLabel: t("logs.delete") }))) return;
  for (const n of names) { try { await api("/api/logs/" + encodeURIComponent(n), { method: "DELETE" }); } catch (e) {} }
  loadLogs();
});
$("#logsPreviewBtn").addEventListener("click", async () => {
  const dec = logsData.filter((f) => logChecked().includes(f.name) && f.kind === "decoded");
  if (dec.length !== 1) { toast(t("logs.pickOneDecoded"), "warn"); return; }
  const name = dec[0].name;
  try {
    const d = await api("/api/logs/" + encodeURIComponent(name) + "/data");
    openChart(name, d.text);
  } catch (e) {
    toast(e.message === "too_large" ? t("logs.tooLarge") : t("banner.error") + " " + e.message, "err");
  }
});

// ---------- log preview chart (canvas, zoom/pan, normalized series) ----------
const CHART_COLORS = ["#2f6fd0", "#1f9d63", "#d33f3f", "#b9740a", "#7d3ac1", "#0aa2a2", "#c02f7a", "#5a7a1f", "#8a5a2b", "#3a5bd0"];
const chart = { cv: null, ctx: null, xs: [], keys: [], cols: {}, maxAbs: {}, min: {}, enabled: new Set(), color: {}, x0: 0, x1: 1, drag: false, px: 0, hoverT: null };
const nearestIdx = (xs, t) => {
  let lo = 0, hi = xs.length - 1; if (hi < 0) return -1;
  while (lo < hi) { const m = (lo + hi) >> 1; if (xs[m] < t) lo = m + 1; else hi = m; }
  return lo > 0 && Math.abs(xs[lo - 1] - t) <= Math.abs(xs[lo] - t) ? lo - 1 : lo;
};
const fmtNum = (v) => (Number.isFinite(v) ? String(+v.toFixed(3)) : "—");
const plabel = (k) => (T["param." + k] || window.I18N.en["param." + k] || k);

// CSV line splitter that respects double-quoted fields (GuzziDiag quotes every
// value); strips the surrounding quotes and unescapes "" -> ".
function splitCSVLine(line) {
  const out = []; let cur = "", q = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (q) {
      if (c === '"') { if (line[i + 1] === '"') { cur += '"'; i++; } else q = false; }
      else cur += c;
    } else if (c === '"') q = true;
    else if (c === ",") { out.push(cur); cur = ""; }
    else cur += c;
  }
  out.push(cur);
  return out.map((s) => s.trim());
}

function parseCSV(text) {
  const lines = text.split(/\r?\n/);
  let hi = 0; while (hi < lines.length && !lines[hi].trim()) hi++;
  const header = splitCSVLine(lines[hi] || "time");
  const keys = header.slice(1);                        // series names from the log itself
  const isMs = (header[0] || "").toLowerCase().includes("ms");  // GuzziDiag "Time in ms"
  const xs = [], cols = keys.map(() => []);
  let t0 = null, idx = 0;
  for (let i = hi + 1; i < lines.length; i++) {
    const ln = lines[i]; if (!ln.trim()) continue;
    const p = splitCSVLine(ln);
    const tf = p[0] || "", tn = Number(tf);
    let x;
    if (tf !== "" && Number.isFinite(tn)) {            // numeric time (ms or s)
      if (t0 === null) t0 = tn;
      x = (tn - t0) / (isMs ? 1000 : 1);
    } else {                                            // ISO timestamp (our logger)
      let ts = Date.parse(tf); if (isNaN(ts)) ts = idx * 1000;
      if (t0 === null) t0 = ts;
      x = (ts - t0) / 1000;
    }
    xs.push(x);
    for (let c = 0; c < keys.length; c++) {
      const v = parseFloat(p[c + 1]);                   // "Coldstart" etc -> NaN (skipped)
      cols[c].push(Number.isFinite(v) ? v : NaN);
    }
    idx++;
  }
  if (xs.length > 3000) {
    const step = Math.ceil(xs.length / 3000), X = [], C = keys.map(() => []);
    for (let i = 0; i < xs.length; i += step) { X.push(xs[i]); keys.forEach((_, c) => C[c].push(cols[c][i])); }
    return { keys, xs: X, cols: C };
  }
  return { keys, xs, cols };
}

function openChart(name, text) {
  const parsed = parseCSV(text);
  const keys = [], cols = {};
  parsed.keys.forEach((k, i) => { if (k && k.trim()) { keys.push(k); cols[k] = parsed.cols[i]; } });
  chart.keys = keys; chart.xs = parsed.xs; chart.cols = cols; chart.maxAbs = {}; chart.min = {}; chart.color = {}; chart.enabled = new Set(); chart.hoverT = null;
  keys.forEach((k, i) => {
    let m = 0, mn = Infinity;
    for (const v of cols[k]) if (Number.isFinite(v)) { m = Math.max(m, Math.abs(v)); mn = Math.min(mn, v); }
    chart.maxAbs[k] = m || 1; chart.min[k] = Number.isFinite(mn) ? mn : 0;
    chart.color[k] = CHART_COLORS[i % CHART_COLORS.length];
    if (m > 0) chart.enabled.add(k);
  });
  chart.x0 = parsed.xs.length ? parsed.xs[0] : 0;
  chart.x1 = parsed.xs.length ? parsed.xs[parsed.xs.length - 1] : 1;
  if (chart.x1 <= chart.x0) chart.x1 = chart.x0 + 1;
  $("#logChartName").textContent = name;
  $("#logChartCard").hidden = false;
  renderChartVars();
  setupChartCanvas();
  requestAnimationFrame(drawChart);   // wait for layout so the canvas has a size
  $("#logChartCard").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderChartVars() {
  const box = $("#logChartVars"); box.innerHTML = "";
  chart.keys.forEach((k) => {
    const on = chart.enabled.has(k);
    const el = document.createElement("label");
    el.className = "chart-var" + (on ? "" : " off");
    el.innerHTML = `<input type="checkbox"${on ? " checked" : ""} /><span class="sw" style="background:${chart.color[k]}"></span>${plabel(k)}`;
    $("input", el).addEventListener("change", (e) => {
      if (e.target.checked) chart.enabled.add(k); else chart.enabled.delete(k);
      el.classList.toggle("off", !e.target.checked);
      drawChart();
    });
    box.appendChild(el);
  });
}

function setupChartCanvas() {
  const cv = $("#logChart"); chart.cv = cv; chart.ctx = cv.getContext("2d");
  if (cv._wired) return; cv._wired = true;
  cv.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = cv.getBoundingClientRect();
    const cx = chart.x0 + ((e.clientX - r.left) / r.width) * (chart.x1 - chart.x0);
    const f = e.deltaY < 0 ? 0.85 : 1.18;
    chart.x0 = cx - (cx - chart.x0) * f; chart.x1 = cx + (chart.x1 - cx) * f;
    drawChart();
  }, { passive: false });
  cv.addEventListener("pointerdown", (e) => { chart.drag = true; chart.px = e.clientX; cv.setPointerCapture(e.pointerId); });
  cv.addEventListener("pointermove", (e) => {
    const r = cv.getBoundingClientRect();
    if (chart.drag) {
      const dt = ((e.clientX - chart.px) / r.width) * (chart.x1 - chart.x0);
      chart.x0 -= dt; chart.x1 -= dt; chart.px = e.clientX; chart.hoverT = null; drawChart();
      return;
    }
    chart.hoverT = chart.x0 + ((e.clientX - r.left) / r.width) * (chart.x1 - chart.x0);  // hover readout
    drawChart();
  });
  cv.addEventListener("pointerup", () => { chart.drag = false; });
  cv.addEventListener("pointercancel", () => { chart.drag = false; });
  cv.addEventListener("pointerleave", () => { chart.hoverT = null; drawChart(); });
  window.addEventListener("resize", () => { if (!$("#logChartCard").hidden) drawChart(); });
}

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function drawChart() {
  const cv = chart.cv, ctx = chart.ctx; if (!cv) return;
  const dpr = window.devicePixelRatio || 1;
  const cssW = cv.clientWidth, cssH = cv.clientHeight;
  if (cssW < 2 || cssH < 2) { requestAnimationFrame(drawChart); return; }  // layout not ready
  cv.width = Math.round(cssW * dpr); cv.height = Math.round(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  const padL = 8, padR = 8, padT = 10, padB = 22;
  const W = cssW - padL - padR, H = cssH - padT - padB;
  const x0 = chart.x0, x1 = chart.x1, span = (x1 - x0) || 1;
  // extend below zero only as far as the actual deepest trough (asymmetric axis) so
  // the mostly-positive series keep most of the height. Series draw as v/maxAbs.
  let minNorm = 0;
  chart.keys.forEach((k) => {
    if (chart.enabled.has(k) && chart.min[k] < 0) minNorm = Math.min(minNorm, chart.min[k] / chart.maxAbs[k]);
  });
  const hasNeg = minNorm < 0;
  const yTop = 1.08, yBot = hasNeg ? Math.max(-1.08, minNorm * 1.12 - 0.02) : -0.03;
  const grid = hasNeg ? (minNorm < -0.1 ? [1, 0.5, 0, minNorm] : [1, 0.5, 0]) : [1, 0.75, 0.5, 0.25, 0];
  const py = (v) => padT + ((yTop - v) / (yTop - yBot)) * H;
  const px = (x) => padL + ((x - x0) / span) * W;
  ctx.strokeStyle = cssVar("--border") || "#ccc"; ctx.lineWidth = 1;
  grid.forEach((gy) => {
    const Y = py(gy); ctx.globalAlpha = gy === 0 ? 0.9 : 0.35;
    ctx.beginPath(); ctx.moveTo(padL, Y); ctx.lineTo(padL + W, Y); ctx.stroke();
  });
  ctx.globalAlpha = 1;
  ctx.fillStyle = cssVar("--muted") || "#888"; ctx.font = "10px system-ui"; ctx.textAlign = "center";
  const nT = 6, dp = span < 5 ? 2 : span < 60 ? 1 : 0;
  for (let i = 0; i <= nT; i++) { const x = x0 + (span * i) / nT; ctx.fillText(x.toFixed(dp) + "s", px(x), padT + H + 14); }
  chart.keys.forEach((k) => {
    if (!chart.enabled.has(k)) return;
    const col = chart.cols[k], m = chart.maxAbs[k];
    ctx.strokeStyle = chart.color[k]; ctx.lineWidth = 1.5; ctx.beginPath();
    let pen = false;
    for (let i = 0; i < chart.xs.length; i++) {
      const xv = chart.xs[i];
      if (xv < x0 - span || xv > x1 + span) { pen = false; continue; }
      const v = col[i]; if (!Number.isFinite(v)) { pen = false; continue; }
      const X = px(xv), Y = py(v / m);
      if (!pen) { ctx.moveTo(X, Y); pen = true; } else ctx.lineTo(X, Y);
    }
    ctx.stroke();
  });

  // hover: crosshair + readout of the actual values at the nearest sample
  if (chart.hoverT !== null && chart.hoverT >= x0 && chart.hoverT <= x1 && chart.xs.length) {
    const idx = nearestIdx(chart.xs, chart.hoverT), tx = chart.xs[idx], hx = px(tx);
    ctx.strokeStyle = cssVar("--muted") || "#888"; ctx.globalAlpha = 0.6; ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.moveTo(hx, padT); ctx.lineTo(hx, padT + H); ctx.stroke();
    ctx.setLineDash([]); ctx.globalAlpha = 1;
    const rows = [{ c: null, t: tx.toFixed(dp) + " s" }];
    chart.keys.forEach((k) => {
      if (!chart.enabled.has(k)) return;
      const v = chart.cols[k][idx];
      if (Number.isFinite(v)) { ctx.fillStyle = chart.color[k]; ctx.beginPath(); ctx.arc(hx, py(v / chart.maxAbs[k]), 3, 0, 7); ctx.fill(); }
      rows.push({ c: chart.color[k], t: `${k}: ${fmtNum(v)}` });
    });
    ctx.font = "11px system-ui"; ctx.textAlign = "left"; ctx.textBaseline = "top";
    let bw = 0; rows.forEach((r) => { bw = Math.max(bw, ctx.measureText(r.t).width); });
    const pb = 8, lh = 15, boxW = bw + pb * 2 + 14, boxH = rows.length * lh + pb * 2;
    let bx = hx + 10; if (bx + boxW > padL + W) bx = hx - 10 - boxW; if (bx < padL) bx = padL + 2;
    const by = padT + 6;
    ctx.globalAlpha = 0.96; ctx.fillStyle = cssVar("--surface") || "#fff"; ctx.strokeStyle = cssVar("--border") || "#ccc";
    ctx.beginPath(); ctx.rect(bx, by, boxW, boxH); ctx.fill(); ctx.stroke(); ctx.globalAlpha = 1;
    rows.forEach((r, i) => {
      const ty = by + pb + i * lh;
      if (r.c) { ctx.fillStyle = r.c; ctx.fillRect(bx + pb, ty + 2, 9, 9); }
      ctx.fillStyle = cssVar("--fg") || "#000"; ctx.fillText(r.t, bx + pb + 14, ty);
    });
    ctx.textBaseline = "alphabetic";
  }
}

$("#logChartClose").addEventListener("click", () => { $("#logChartCard").hidden = true; });

// ---------- firmware ----------
let fwTimer = null;
let fwSuggest = null;            // {for, name} offered after a read
const fwSuggestSkip = new Set(); // dismissed client-side; the 1.5 s poll must not resurrect it
let fwGuardVerdict = null;       // last /api/firmware/check answer for the selected file
let fwReadNameTouched = false;   // once the rider edits the name, stop proposing one
let fwFiles = [];
let fwActive = null;
let fwSortKey = "mtime";
let fwSortDir = -1;
let fwRequiredSize = 0;
let fwBusy = false;
let fwAvailable = false;

// The ECU hands over its Drawing at connect, so a name can be offered long before
// the 20-minute read finishes. Only ever fills an untouched, unfocused field —
// the rider stays in charge of the file name.
function proposeReadName(drawing) {
  const el = $("#fwReadName");
  if (!el || fwReadNameTouched || document.activeElement === el) return;
  const d = new Date();
  const stamp = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
  el.value = drawing ? `${drawing}-${stamp}.bin` : "";
}
$("#fwReadName").addEventListener("input", () => { fwReadNameTouched = true; });

function fwEnter() { fwSortArrows(); loadFirmware(); loadFwCatalog(); fwTimer = setInterval(loadFirmware, 1500); }
function fwLeave() { if (fwTimer) { clearInterval(fwTimer); fwTimer = null; } }

async function loadFirmware() {
  let d;
  try { d = await api("/api/firmware"); } catch (e) { return; }
  $("#fwNa").hidden = !!d.available;
  setDiskFree($("#fwFree"), d);
  const busy = d.op && d.op !== "idle";
  const res = d.result ? " · " + t("fw." + d.result) : "";
  const cur = d.current ? " · " + d.current : "";
  $("#fwState").textContent = t("fw." + (d.op || "idle")) + cur + res;
  const log = $("#fwLog");
  log.textContent = (d.log || []).join("\n");
  log.scrollTop = log.scrollHeight;
  $("#fwCancel").hidden = !busy;
  fwBusy = busy; fwAvailable = !!d.available;
  fwRequiredSize = d.required_size || 0;
  $("#fwOverrideActive").hidden = !d.guard_override;
  renderFwSuggest(d.suggest);
  $("#fwReadBtn").disabled = busy || !d.available;

  fwFiles = d.files || [];
  const sel = $("#fwWriteSelect"), keep = sel.value;
  sel.innerHTML = fwFiles.length ? "" : '<option value="">—</option>';
  fwFiles.forEach((f) => { const o = document.createElement("option"); o.value = o.textContent = f.name; sel.appendChild(o); });
  if (fwFiles.some((f) => f.name === keep)) sel.value = keep;
  if (fwActive && !fwFiles.some((f) => f.name === fwActive)) { fwActive = null; clearDesc(); }
  renderFwFiles();
  loadWriteDesc();
  checkWriteGuard();
}

// Refuse to flash anything but an exact-size image (mismatched size bricks the ECU),
// then ask the board whether this calibration belongs in the ECU on the bus. The
// server decides; this only lets the rider see it before pressing the button.
function checkWriteGuard() {
  const f = fwFiles.find((x) => x.name === $("#fwWriteSelect").value);
  const warn = $("#fwSizeWarn"), btn = $("#fwWriteBtn");
  const bad = f && fwRequiredSize && f.size !== fwRequiredSize;
  if (bad) {
    warn.textContent = `⚠ ${t("fw.sizeMismatch")}: ${fmtSize(f.size)} ≠ ${fmtSize(fwRequiredSize)} (${fwRequiredSize} B)`;
    warn.hidden = false;
  } else {
    warn.hidden = true;
  }
  btn.disabled = fwBusy || !fwAvailable || !f || bad;
  loadGuard(f && !bad ? f.name : "");
}

async function loadGuard(name) {
  const box = $("#fwGuard");
  if (!name) { fwGuardVerdict = null; box.hidden = true; return; }
  let v;
  try { v = await api("/api/firmware/check?name=" + encodeURIComponent(name)); }
  catch (e) { fwGuardVerdict = null; box.hidden = true; return; }
  if ($("#fwWriteSelect").value !== name) return;   // selection moved while we asked
  fwGuardVerdict = v;
  if (v.level === "ok") { box.hidden = true; return; }
  box.className = "banner " + (v.level === "block" ? "err" : "warn");
  box.textContent = "⚠ " + t("fw.guard." + v.reason) + guardPair(v);
  box.hidden = false;
  if (v.level === "block") $("#fwWriteBtn").disabled = true;
}

// "image 23ECCLGPSMC · ECU 23ECCLGPSMD" — the two codes are the whole argument
function guardPair(v) {
  const a = (v.image && v.image.code) || "—", b = (v.ecu && v.ecu.code) || "—";
  return ` (${t("fw.guardImage")} ${a} · ${t("fw.guardEcu")} ${b})`;
}

// The identity the image claims about itself, plus what the catalog makes of it.
// A second line rather than a column: a fifth column crushes a 360 px phone.
function fwIdentLine(f) {
  const id = f.ident || {};
  if (!id.code) {
    const why = id.reason && id.reason !== "busy" ? ` (${esc(id.reason)})` : "";
    return `<span class="fw-ident fw-ident--none">${t("fw.unknownCode")}${why}</span>`;
  }
  const bike = [id.brand, id.model].filter(Boolean).join(" ");
  // the revision note and the catalog's own note ride in the tooltip: the guard
  // compares brand+model, so nothing here may end up looking like another bike
  const tip = [id.rev_note, id.note, id.verified ? "" : t("fw.unverified")]
    .filter(Boolean).join(" · ");
  const cls = "fw-ident" + (id.verified ? "" : " fw-ident--soft");
  const title = tip ? ` title="${esc(tip)}"` : "";
  const badges = (f.mismatch || []).map((m) =>
    `<span class="fw-badge" title="${esc(t("fw.mismatch." + m))}">⚠</span>`).join("");
  return `<span class="${cls}"${title}><b>${esc(id.code)}</b>` +
    (bike ? ` · ${esc(bike)}` : "") + (id.verified ? "" : " ·&nbsp;?") + `</span>${badges}`;
}

function renderFwFiles() {
  const list = $("#fwList");
  const checked = new Set([...list.querySelectorAll(".fwsel:checked")].map((c) => c.value));
  const k = fwSortKey, dir = fwSortDir;
  const rows = fwFiles.slice().sort((a, b) => {
    let x = a[k], y = b[k];
    if (k === "name") { x = String(x).toLowerCase(); y = String(y).toLowerCase(); }
    return x < y ? -dir : x > y ? dir : 0;
  });
  list.innerHTML = "";
  if (!rows.length) { list.innerHTML = `<p class="hint">${t("fw.empty")}</p>`; return; }
  rows.forEach((f) => {
    const row = document.createElement("div");
    row.className = "fwrow" + (f.name === fwActive ? " active" : "");
    const date = fmtDate(f.mtime);
    row.innerHTML =
      `<input type="checkbox" class="fwsel" value="${esc(f.name)}"${checked.has(f.name) ? " checked" : ""} />` +
      `<span class="fw-file__cell"><span class="fw-file__name" data-pick="${esc(f.name)}">${esc(f.name)}</span>` +
      fwIdentLine(f) + `</span>` +
      `<span class="col-date">${date || ""}</span>` +
      `<span class="col-size${fwRequiredSize && f.size !== fwRequiredSize ? " bad" : ""}" title="${f.size} B">${fmtSize(f.size)}</span>`;
    list.appendChild(row);
  });
  list.querySelectorAll("[data-pick]").forEach((s) =>
    s.addEventListener("click", () => setActive(s.dataset.pick))
  );
}

function renderFwSuggest(sug) {
  const box = $("#fwSuggest");
  fwSuggest = sug && !fwSuggestSkip.has(sug.for) ? sug : null;
  box.hidden = !fwSuggest;
  if (fwSuggest && document.activeElement !== $("#fwSuggestName")) {
    $("#fwSuggestName").value = fwSuggest.name;
  }
}

$("#fwSuggestNo").addEventListener("click", () => {
  if (fwSuggest) fwSuggestSkip.add(fwSuggest.for);
  renderFwSuggest(null);
});
$("#fwSuggestOk").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
  if (!fwSuggest) return;
  const from = fwSuggest.for, to = $("#fwSuggestName").value.trim();
  try {
    const r = await api("/api/firmware/rename", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from, to }),
    });
    fwSuggestSkip.add(from);
    if (fwActive === from) setActive(r.name);
    toast(t("fw.renamed") + " " + r.name, "ok");
  } catch (err) { toastErr(err); }
  loadFirmware();
}));

function setActive(name) {
  fwActive = name;
  // picking an unidentified file pre-loads the catalog form with its code, so
  // naming the bike is one field away from noticing it is missing
  const f = fwFiles.find((x) => x.name === name);
  const code = f && f.ident ? f.ident.code : "";
  if (code && !(f.ident.brand || f.ident.model) && !$("#fwCatCode").value) {
    $("#fwCatCode").value = code;
  }
  $("#fwWriteSelect").value = name;
  loadWriteDesc();
  checkWriteGuard();
  [...$("#fwList").children].forEach((r) =>
    r.classList.toggle("active", r.querySelector("[data-pick]")?.dataset.pick === name)
  );
  loadDesc(name);
}

function clearDesc() {
  $("#fwDescName").textContent = "—";
  const ta = $("#fwDesc"); ta.value = ""; ta.disabled = true;
  $("#fwDescSave").disabled = true;
}

async function loadDesc(name) {
  const ta = $("#fwDesc");
  $("#fwDescName").textContent = name;
  ta.disabled = false; $("#fwDescSave").disabled = false;
  try { const d = await api("/api/firmware/desc/" + encodeURIComponent(name)); ta.value = d.text || ""; }
  catch (e) { ta.value = ""; }
}

$("#fwDescSave").addEventListener("click", async () => {
  if (!fwActive) return;
  try {
    await api("/api/firmware/desc/" + encodeURIComponent(fwActive), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: $("#fwDesc").value }),
    });
    loadFirmware();
  } catch (e) { toastErr(e); }
});

function fwSortArrows() {
  $$("#fwSort button").forEach((el) => {
    const arr = el.querySelector(".arrow");
    if (el.dataset.s === fwSortKey) { el.classList.add("on"); if (arr) arr.textContent = fwSortDir > 0 ? "▲" : "▼"; }
    else { el.classList.remove("on"); if (arr) arr.textContent = ""; }
  });
}
$("#fwSort").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  const k = b.dataset.s;
  if (fwSortKey === k) fwSortDir = -fwSortDir;
  else { fwSortKey = k; fwSortDir = k === "name" ? 1 : -1; }
  fwSortArrows();
  renderFwFiles();
});

async function loadWriteDesc() {
  const name = $("#fwWriteSelect").value;
  const pre = $("#fwWriteDesc");
  if (!name) { pre.hidden = true; pre.textContent = ""; return; }
  try {
    const d = await api("/api/firmware/desc/" + encodeURIComponent(name));
    pre.textContent = d.text || "";
    pre.hidden = !d.text;
  } catch (e) { pre.hidden = true; }
}
$("#fwWriteSelect").addEventListener("change", () => { loadWriteDesc(); checkWriteSize(); });

const fwChecked = () => [...document.querySelectorAll("#fwList .fwsel:checked")].map((c) => c.value);

$("#fwDownloadBtn").addEventListener("click", () => {
  fwChecked().forEach((n) => {
    const a = document.createElement("a");
    a.href = "/api/firmware/files/" + encodeURIComponent(n);
    a.download = n;
    document.body.appendChild(a); a.click(); a.remove();
  });
});
$("#fwDiffBtn").addEventListener("click", async () => {
  const names = fwChecked();
  const res = $("#fwDiffResult");
  if (names.length !== 2) { res.textContent = t("fw.selectTwo"); return; }
  try {
    const d = await api(`/api/firmware/diff?a=${encodeURIComponent(names[0])}&b=${encodeURIComponent(names[1])}`);
    res.textContent = d.equal
      ? t("fw.diffEqual")
      : `${t("fw.diffDiff")} ${d.diff_bytes} ${t("fw.bytes")} (A ${fmtSize(d.size_a)} · B ${fmtSize(d.size_b)})`;
  } catch (e) { res.textContent = t("banner.error") + " " + e.message; }
});

$("#fwReadBtn").addEventListener("click", async () => {
  const verbose = $("#fwVerbose").checked;
  try {
    await api("/api/firmware/read", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: $("#fwReadName").value.trim(), verbose }) });
    loadFirmware();
  } catch (e) { toastErr(e); }
});
$("#fwWriteBtn").addEventListener("click", async () => {
  const name = $("#fwWriteSelect").value;
  if (!name) return;
  // the one action that can brick the ECU — the file name and, when the image does
  // not match the ECU on the bus, the reason both go into the question
  let ask = `${t("fw.confirmWrite")}\n\n${name}`;
  if (fwGuardVerdict && fwGuardVerdict.level !== "ok") {
    ask += `\n\n⚠ ${t("fw.guard." + fwGuardVerdict.reason)}${guardPair(fwGuardVerdict)}`;
  }
  if (!(await confirmDialog(ask, { danger: true, okLabel: t("fw.write") }))) return;
  const verbose = $("#fwVerbose").checked;
  try {
    await api("/api/firmware/write", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, verbose }) });
    loadFirmware();
  } catch (e) {
    const msg = e.message === "size_mismatch" ? t("fw.sizeMismatch")
      : e.message.startsWith("fw_") ? t("fw.guard." + e.message.slice(3))
      : t("banner.error") + " " + e.message;
    toast(msg, "err");
  }
});
$("#fwDeleteBtn").addEventListener("click", async () => {
  const names = fwChecked();
  if (!names.length) return;
  if (!(await confirmDialog(t("fw.confirmDelete"), { danger: true, okLabel: t("fw.deleteBtn") }))) return;
  for (const n of names) {
    try { await api("/api/firmware/files/" + encodeURIComponent(n), { method: "DELETE" }); } catch (e) {}
  }
  loadFirmware();
});
$("#fwCancel").addEventListener("click", async () => {
  if (!(await confirmDialog(t("fw.confirmCancel"), { danger: true, okLabel: t("fw.cancel") }))) return;
  try { await api("/api/firmware/cancel", { method: "POST" }); loadFirmware(); } catch (e) {}
});
// ---------- calibration catalog ----------
const CAT_ERR = {
  fw_catalog_readonly: "fw.catalogReadonly",
  fw_catalog_seed: "fw.catalogSeed",
  fw_bad_code: "fw.catalogNeedCode",
};
const catErr = (err) =>
  CAT_ERR[err.message] ? t(CAT_ERR[err.message]) : t("banner.error") + " " + err.message;

// A few hundred entries, so the catalog is a collapsed tree — manufacturer, then
// model, then the codes — with a search that filters by the firmware code. A
// search opens the groups it matched; groups opened by hand stay open across
// re-renders (fwCatOpen).
let fwCatEntries = [];
let fwCatQuery = "";
const fwCatOpen = new Set();

async function loadFwCatalog() {
  try { const d = await api("/api/firmware/catalog"); fwCatEntries = d.entries || []; }
  catch (e) { return; }
  renderFwCatalog();
}

// A full code finds the family entry covering it: searching 23ECCLGPSMD must land
// on 23ECCLGPSM, whose revisions carry the D.
function fwCatMatches(e, q) {
  if (!q) return true;
  const code = String(e.code || "").toUpperCase();
  if (code.includes(q) || q.startsWith(code)) return true;
  return Object.keys(e.revisions || {}).some((r) => (code + r.toUpperCase()).includes(q));
}

function fwCatRow(e) {
  const tip = [e.note, e.source].filter(Boolean).join(" · ");
  const title = tip ? ` title="${esc(tip)}"` : "";
  const mark = e.verified
    ? `<span class="fw-cat-ok" title="${esc(t("fw.verified"))}">✓</span>`
    : `<span class="fw-ident--soft"> ·&nbsp;${esc(t("fw.unverified"))}</span>`;
  const revs = Object.entries(e.revisions || {})
    .map(([r, d]) => `${esc(e.code)}${esc(r)}${d ? " — " + esc(d) : ""}`).join(", ");
  const revLine = revs ? `<div class="fw-cat-revs">${revs}</div>` : "";
  const del = e.user
    ? `<button class="mini mini--danger" data-del="${esc(e.code)}" data-space="${esc(e.space || "image")}">${esc(t("fw.catalogDelete"))}</button>`
    : "";
  return `<div class="fw-cat-row"${title}><span><b>${esc(e.code)}</b>` +
    ` <span class="fw-ident--soft">(${esc(e.space || "image")})</span>${mark}${revLine}</span>${del}</div>`;
}

function renderFwCatalog() {
  const box = $("#fwCatalogList");
  const q = fwCatQuery.trim().toUpperCase();
  const rows = fwCatEntries.filter((e) => fwCatMatches(e, q));
  const count = $("#fwCatCount");
  if (count) {
    count.textContent = q ? `${rows.length} / ${fwCatEntries.length}` : String(fwCatEntries.length);
  }
  if (!rows.length) {
    box.innerHTML = `<p class="hint">${fwCatEntries.length ? t("fw.catalogNoMatch") : t("fw.catalogEmpty")}</p>`;
    return;
  }
  const tree = new Map();
  rows.forEach((e) => {
    const brand = e.brand || "—";
    const model = e.model || "—";
    if (!tree.has(brand)) tree.set(brand, new Map());
    const models = tree.get(brand);
    if (!models.has(model)) models.set(model, []);
    models.get(model).push(e);
  });
  const cmp = (a, b) => String(a).localeCompare(String(b), undefined, { numeric: true });
  box.innerHTML = [...tree.keys()].sort(cmp).map((brand) => {
    const models = tree.get(brand);
    const total = [...models.values()].reduce((n, xs) => n + xs.length, 0);
    const bOpen = q || fwCatOpen.has("b:" + brand) ? " open" : "";
    const inner = [...models.keys()].sort(cmp).map((model) => {
      const list = models.get(model).slice().sort((a, b) => cmp(a.code, b.code));
      const mOpen = q || fwCatOpen.has(`m:${brand}/${model}`) ? " open" : "";
      return `<details class="fw-cat-model" data-key="m:${esc(brand)}/${esc(model)}"${mOpen}>` +
        `<summary>${esc(model)}<span class="fw-cat-count">${list.length}</span></summary>` +
        list.map(fwCatRow).join("") + `</details>`;
    }).join("");
    return `<details class="fw-cat-brand" data-key="b:${esc(brand)}"${bOpen}>` +
      `<summary>${esc(brand)}<span class="fw-cat-count">${total}</span></summary>${inner}</details>`;
  }).join("");

  box.querySelectorAll("details[data-key]").forEach((d) =>
    d.addEventListener("toggle", () => {
      if (d.open) fwCatOpen.add(d.dataset.key); else fwCatOpen.delete(d.dataset.key);
    }));
  box.querySelectorAll("[data-del]").forEach((b) =>
    b.addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      try {
        await api(`/api/firmware/catalog/${encodeURIComponent(b.dataset.del)}?space=${encodeURIComponent(b.dataset.space)}`,
                  { method: "DELETE" });
      } catch (err) { toast(catErr(err), "err"); }
      loadFwCatalog(); loadFirmware();
    })));
}

$("#fwCatSearch").addEventListener("input", (e) => {
  fwCatQuery = e.target.value;
  renderFwCatalog();
});

$("#fwCatSave").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
  const code = $("#fwCatCode").value.trim();
  if (!code) { toast(t("fw.catalogNeedCode"), "warn"); return; }
  try {
    await api("/api/firmware/catalog", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        code, brand: $("#fwCatBrand").value.trim(), model: $("#fwCatModel").value.trim(),
        space: $("#fwCatSpace").value,
      }),
    });
    $("#fwCatCode").value = ""; $("#fwCatBrand").value = ""; $("#fwCatModel").value = "";
    toast(t("fw.catalogSaved"), "ok");
  } catch (err) { toast(catErr(err), "err"); }
  loadFwCatalog(); loadFirmware();
}));

$("#fwUpload").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try { await api("/api/firmware/upload", { method: "POST", body: fd }); e.target.value = ""; loadFirmware(); }
  catch (err) {
    e.target.value = "";
    const msg = err.message === "no_firmware_in_zip" ? t("fw.noFilesInZip")
      : err.message === "bad_zip" ? t("fw.badZip")
      : t("banner.error") + " " + err.message;
    toast(msg, "err");
  }
});

// ---------- boot ----------
async function init() {
  let loc = "en", cfg = null;
  let savedTheme = "auto";
  try { savedTheme = localStorage.getItem("theme") || "auto"; } catch (e) {}
  applyTheme(savedTheme);
  try { cfg = await api("/api/config"); loc = cfg.locale || "en"; } catch (e) {}
  applyLocale(loc);
  // No battery-backed RTC on the board: offer this browser's clock right away.
  // Fire-and-forget — the server keeps the once-per-power-up marker, so racing
  // tabs (or a reload) cannot set the clock twice.
  if (!cfg || cfg.system?.auto_time_sync !== false) {
    api("/api/system/time/auto", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        epoch: Date.now() / 1000,
        tz: Intl.DateTimeFormat().resolvedOptions().timeZone || "",
      }),
    }).catch(() => {});
  }
  // status maps ride along with the actuator catalog and are needed on the Logger
  // tab too (mapped channels render as text), so fetch them at boot
  loadActuators();
  connectWS();
}
init();
