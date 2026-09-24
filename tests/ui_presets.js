"use strict";
// Headless harness for the three parameter presets on the Logger tab.
//
// The point of the feature is that no "active preset" is stored anywhere: a
// button lights up when the live selection equals its key set, so a hand-made
// tick puts it out by itself. These tests drive the buttons and the checkboxes
// through their own handlers and watch what goes out on /api/selected and
// /api/presets.
//
// The list is the same in both modes — a channel can be ticked at any moment —
// so what the switch decides is only where a tick lands: the live selection
// alone, or the preset slot open for editing as well.
//
// app/static/app.js is evaluated whole in a vm on the shared DOM stub. Rows are
// built with innerHTML, so their checkbox is the stub's per-selector node: the
// harness sets `.checked` on it exactly as a tap does.
//
// Run directly: node tests/ui_presets.js   (also driven by test_ui_presets.py)

const { makeSandbox: baseSandbox, read } = require("./ui_sandbox");

const CAT = [
  { key: "rpm", name: "RPM", unit: "rpm", rli: 48, default: true, group: "known", map: "", map_type: "" },
  { key: "vbat", name: "Voltage", unit: "V", rli: 60, default: true, group: "known", map: "", map_type: "" },
  { key: "tmot", name: "Coolant", unit: "C", rli: 49, default: true, group: "known", map: "", map_type: "" },
  { key: "lambda", name: "Lambda", unit: "", rli: 80, default: false, group: "check", map: "", map_type: "" },
  { key: "r31", name: "rli 0x31", unit: "", rli: 49, default: false, group: "unknown", map: "", map_type: "" },
  { key: "r36", name: "rli 0x36", unit: "", rli: 54, default: false, group: "unknown", map: "", map_type: "" },
];

const PRESETS = () => [
  { name: "Dyno", keys: ["rpm", "tmot"] },
  { name: "", keys: ["rpm", "r36"] },          // reaches into the folded list
  { name: "", keys: [] },                      // empty slot
];

const SNAP = (over = {}) => ({
  status: "connected", status_msg: "", ecu_hw: "IAW5AMHW610", ecu_id: "IAW5AM", ecu_desc: "IAW 5AM",
  ecu_fields: {}, poll_hz: 1.2, bus_baud: 10400,
  logging_decoded: false, logging_raw: false,
  log_decoded_file: "", log_decoded_records: 0, log_raw_file: "", log_raw_records: 0,
  storage_root: "/root/k-line", storage_dest: "internal", storage_fallback: false,
  scan_on: false, scan_total: 0, scan_pos: 0, scan_alive: 0, scan_remaining: -1, scan_sweeps: 0,
  wifi_mode: "ap", wifi_link: {}, ap_channel: 6,
  test_mode: false, test_mode_detail: "", act_key: "", act_lid: 0, act_until: 0,
  poll_req_ms: 0, poll_fixed_ms: 0, poll_min_ms: 150,
  values: {}, values_ts: 0, catalog: CAT, selected: ["rpm"], presets: PRESETS(), ...over,
});

// init() finishes on its own microtasks and repaints the parameter list on the
// way through applyLocale(); the rows the tests hold on to have to be the ones
// that survive that, so every sandbox is drained before the first snapshot.
const drain = async () => { for (let i = 0; i < 8; i++) await new Promise(setImmediate); };

// The board echoes a POSTed preset list back the way /api/presets does, so the
// browser's adopt-the-answer path is exercised too.
async function sandbox(mode = "view") {
  const posts = [];
  const sb = baseSandbox({
    storage: { paramMode: mode },
    fetch(url, opts) {
      const body = opts && opts.body ? JSON.parse(opts.body) : null;
      if (opts && opts.method === "POST") posts.push([url, body]);
      const out = url === "/api/presets" ? { presets: body.presets } : {};
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(out) });
    },
  });
  for (const id of ["#decToggle", "#rawToggle"]) {     // applySnapshot walks up to .rec-row
    const row = sb.document.createElement("div");
    row.classList.add("rec-row");
    row.appendChild(sb.document.querySelector(id));
  }
  await drain();
  posts.length = 0;             // whatever boot did is not what these tests watch
  sb.posts = posts;
  return sb;
}

let failed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }

const buttons = (sb) => sb.document.querySelector("#presets").children;
const rowsOf = (sb) => sb.document.querySelector("#params").querySelectorAll(".prow");
const rowFor = (sb, key) => rowsOf(sb).find((r) => r.dataset.key === key);
const boxFor = (sb, key) => rowFor(sb, key).querySelector("input");
const lastPost = (sb, url) => [...sb.posts].reverse().find(([u]) => u === url);

// a tap on a checkbox: the browser flips it, then fires change
async function tick(sb, key, on) {
  const inp = boxFor(sb, key);
  inp.checked = on;
  await inp.handlers.change[0]({ target: inp });
}
const tap = async (sb, i) => { await buttons(sb)[i].handlers.click[0](); };

test("applying a preset ticks exactly its keys and sends them to the board", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP());
  assert(buttons(sb).length === 3, "three slots, got " + buttons(sb).length);
  await tap(sb, 0);
  const [, body] = lastPost(sb, "/api/selected");
  assert(body.keys.join(",") === "rpm,tmot", "got " + body.keys.join(","));
  assert(boxFor(sb, "vbat").checked === false, "a channel outside the preset is unticked");
  assert(rowFor(sb, "vbat").classList.contains("off"), "and its row is dimmed");
});

test("the applied preset is the green one, and only it", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP());
  await tap(sb, 0);
  const b = buttons(sb);
  assert(b[0].classList.contains("is-on"), "the applied slot lights up");
  assert(b[0].getAttribute("aria-pressed") === "true", "and says so to a screen reader");
  assert(!b[1].classList.contains("is-on"), "the others stay plain");
});

test("a hand-made tick puts the button out, and marks where it came from", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP());
  await tap(sb, 0);
  await tick(sb, "vbat", true);
  const b = buttons(sb);
  assert(!b[0].classList.contains("is-on"), "the set no longer equals the preset");
  assert(b[0].classList.contains("is-was"), "but the button still says which one was left");
  const [, body] = lastPost(sb, "/api/selected");
  assert(body.keys.includes("vbat"), "the new set went out, so the log restarts on it");
});

test("the button carries its name and its channel count", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP());
  const b = buttons(sb);
  assert(b[0].textContent === "Dyno · 2", "named slot, got " + b[0].textContent);
  assert(b[1].textContent === "preset2 · 2", "an unnamed slot still has a label, got " + b[1].textContent);
  assert(b[2].textContent === "preset3 · 0", "an empty slot too, got " + b[2].textContent);
});

test("an empty preset says so instead of clearing the selection", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP());
  const before = sb.posts.length;
  await tap(sb, 2);
  assert(sb.posts.length === before, "nothing may go out for an empty slot");
  assert(buttons(sb)[2].classList.contains("is-empty"), "and it looks empty");
  const toasts = sb.document.querySelector("#toasts").children;
  assert(toasts.length === 1 && toasts[0].textContent === sb.window.I18N.en["preset.empty"],
         "the rider is told why nothing happened");
});

test("a preset that reaches into the folded rli list opens the fold", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP());
  const wrap = sb.document.querySelector("#params").children
    .find((n) => n.classList.contains("unknown-wrap"));
  assert(wrap.hidden === true, "the fold starts closed");
  await tap(sb, 1);
  assert(wrap.hidden === false, "applying r36 must not look like nothing happened");
});

test("presets mode: tapping a slot opens it for editing and shows the name field", async () => {
  const sb = await sandbox("edit");
  sb.applySnapshot(SNAP());
  assert(sb.document.querySelector("#presetEdit").hidden === true, "closed until a slot is tapped");
  await tap(sb, 0);
  assert(buttons(sb)[0].classList.contains("is-edit"), "the open slot is marked");
  assert(sb.document.querySelector("#presetEdit").hidden === false, "the name field appears");
  assert(sb.document.querySelector("#presetName").value === "Dyno", "prefilled with the current name");
  // the hint lives up in the card head now, so it has to be shown from here too
  assert(sb.document.querySelector("#presetHint").hidden === false, "and the hint explains it");
});

test("presets mode: a tick writes straight through into the open preset", async () => {
  const sb = await sandbox("edit");
  sb.applySnapshot(SNAP());
  await tap(sb, 0);
  await tick(sb, "lambda", true);
  const [, body] = lastPost(sb, "/api/presets");
  assert(body.presets[0].keys.join(",") === "rpm,tmot,lambda", "got " + body.presets[0].keys.join(","));
  assert(body.presets[1].keys.join(",") === "rpm,r36", "the other slots are untouched");
  assert(buttons(sb)[0].textContent === "Dyno · 3", "the count follows, got " + buttons(sb)[0].textContent);
});

test("presets mode: tapping the open slot again closes the editor", async () => {
  const sb = await sandbox("edit");
  sb.applySnapshot(SNAP());
  await tap(sb, 0);
  await tap(sb, 0);
  assert(sb.document.querySelector("#presetEdit").hidden === true, "the field goes away");
  assert(sb.document.querySelector("#presetHint").hidden === true, "and so does the hint");
  assert(!buttons(sb)[0].classList.contains("is-edit"), "and the slot is no longer marked");
  const before = sb.posts.length;
  await tick(sb, "lambda", true);
  const [, body] = lastPost(sb, "/api/presets") || [null, null];
  assert(sb.posts.length > before, "the selection still goes out");
  assert(!body || body.presets[0].keys.length === 2, "but no longer into the preset");
});

test("the name is edited live and saved on blur, clipped to 8 characters", async () => {
  const sb = await sandbox("edit");
  sb.applySnapshot(SNAP());
  await tap(sb, 1);
  const inp = sb.document.querySelector("#presetName");
  await inp.handlers.input[0]({ target: { value: "cold start" } });
  assert(buttons(sb)[1].textContent === "cold sta · 2",
         "the button relabels as it is typed, got " + buttons(sb)[1].textContent);
  await inp.handlers.change[0]({ target: inp });
  const [, body] = lastPost(sb, "/api/presets");
  assert(body.presets[1].name === "cold sta", "got " + JSON.stringify(body.presets[1].name));
});

test("the push does not type over the name field while it is being filled in", async () => {
  const sb = await sandbox("edit");
  sb.applySnapshot(SNAP());
  await tap(sb, 1);
  const inp = sb.document.querySelector("#presetName");
  sb.document.activeElement = inp;                 // the rider is in the field
  inp.value = "warmup";
  await inp.handlers.input[0]({ target: inp });
  const other = SNAP();
  other.presets[1].name = "board";                 // a stale name arrives at 5 Hz
  sb.applySnapshot(other);
  assert(inp.value === "warmup", "the field keeps what is being typed, got " + inp.value);
  assert(buttons(sb)[1].textContent === "warmup · 2",
         "and so does the button, got " + buttons(sb)[1].textContent);
});

test("the cost of a set is priced from the measured per-request time", async () => {
  const sb = await sandbox("edit");
  sb.applySnapshot(SNAP({ poll_req_ms: 60 }));
  await tap(sb, 0);                              // rpm + tmot -> two distinct rli
  const cost = sb.document.querySelector("#presetCost");
  assert(sb.document.querySelector("#paramsTools").hidden === false, "the price belongs to the pick mode");
  // 2 requests x 60 ms = 120 ms -> 8.3 Hz, clamped by the 150 ms floor to 6.7
  assert(cost.textContent === "≈ 6.7 Hz · 2 requests", "got " + cost.textContent);
  await tick(sb, "lambda", true);
  await tick(sb, "r36", true);
  // 4 x 60 = 240 ms -> 4.2 Hz, under the floor now
  assert(cost.textContent === "≈ 4.2 Hz · 4 requests", "got " + cost.textContent);
});

test("a cycle's fixed cost is charged once, not once per request", async () => {
  const sb = await sandbox("edit");
  // The defect this pins: pricing a set as n x per ignores the work a cycle does
  // whatever the selection -- derive, CSV write, log reconcile, keepalive -- so
  // the estimate was out by that whole amount, and out by more the bigger the set.
  sb.applySnapshot(SNAP({ poll_req_ms: 60, poll_fixed_ms: 200 }));
  await tap(sb, 0);                              // rpm + tmot -> two distinct rli
  const cost = sb.document.querySelector("#presetCost");
  // 200 + 2 x 60 = 320 ms -> 3.1 Hz.  Linear would have said 6.7.
  assert(cost.textContent === "\u2248 3.1 Hz \u00b7 2 requests", "got " + cost.textContent);
  await tick(sb, "lambda", true);
  await tick(sb, "r36", true);
  // 200 + 4 x 60 = 440 ms -> 2.3 Hz.  Linear would have said 4.2.
  assert(cost.textContent === "\u2248 2.3 Hz \u00b7 4 requests", "got " + cost.textContent);
});

test("with no live bus the price falls back to the wire arithmetic", async () => {
  const sb = await sandbox("edit");
  // poll_req_ms 0: nothing measured. The floor is lifted here so the paper number
  // is what shows, instead of being clamped away by it.
  // poll_fixed_ms is deliberately non-zero here: with nothing measured on the
  // wire there is no fixed half to believe either, and it must not leak in.
  sb.applySnapshot(SNAP({ poll_min_ms: 10, poll_fixed_ms: 200 }));
  await tap(sb, 0);
  // 2 requests x 13.5 ms of wire at 10400 8N1 = 27 ms -> 37 Hz
  assert(sb.document.querySelector("#presetCost").textContent === "≈ 37.0 Hz · 2 requests",
         "got " + sb.document.querySelector("#presetCost").textContent);
});

test("the price and all/none are pick-mode tools, gone in read mode", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP({ poll_req_ms: 60 }));
  assert(sb.document.querySelector("#paramsTools").hidden === true, "read mode is for reading");
  // the name field is grid-laid-out, which used to beat [hidden] and leave it on
  // screen in read mode; renderPresets has to hide it explicitly
  assert(sb.document.querySelector("#presetEdit").hidden === true, "no name field in read mode");
  assert(sb.document.querySelector("#presetHint").hidden === true, "nor its hint");
});

// `hidden` is a UA style, so any author rule that sets `display` on the same
// element wins and the block stays on screen. That is how the name field showed
// up in read mode; three other blocks in style.css carry the same one-line fix.
test("the blocks that lay themselves out cancel display for [hidden]", () => {
  const css = read("app/static/style.css");
  for (const sel of [".preset-edit", ".params-tools"]) {
    assert(new RegExp(`\\${sel}\\[hidden\\]\\s*{[^}]*display:\\s*none`).test(css),
           `${sel} sets display, so it needs a ${sel}[hidden] { display: none } of its own`);
  }
});

test("the name field sits in the column of the button it renames", async () => {
  const sb = await sandbox("edit");
  sb.applySnapshot(SNAP());
  await tap(sb, 1);
  assert(sb.document.querySelector("#presetName").style.gridColumn === "2",
         "got " + sb.document.querySelector("#presetName").style.gridColumn);
  await tap(sb, 2);
  assert(sb.document.querySelector("#presetName").style.gridColumn === "3",
         "got " + sb.document.querySelector("#presetName").style.gridColumn);
});

test("opening a slot stops the recording and closing it puts it back", async () => {
  const sb = await sandbox("edit");
  sb.applySnapshot(SNAP({ logging_decoded: true, logging_raw: false }));
  await tap(sb, 0);
  const off = lastPost(sb, "/api/logging");
  assert(off && off[1].decoded === false && off[1].raw === false,
         "the slot edit must not be recorded, got " + JSON.stringify(off));
  const dT = sb.document.querySelector("#decToggle");
  assert(dT.disabled === true, "and the switch says it cannot be turned back on");
  assert(sb.document.querySelector("#decMeta").textContent === sb.window.I18N.en["err.busy_preset"],
         "with the reason, got " + sb.document.querySelector("#decMeta").textContent);
  await tap(sb, 0);                              // close the slot
  const back = lastPost(sb, "/api/logging");
  assert(back[1].decoded === true, "the parked intent comes back, got " + JSON.stringify(back));
  assert(sb.document.querySelector("#decToggle").disabled === false, "and the switch is free again");
});

test("a slot edit with nothing recording leaves the switches alone", async () => {
  const sb = await sandbox("edit");
  sb.applySnapshot(SNAP());
  await tap(sb, 0);
  assert(!lastPost(sb, "/api/logging"), "nothing to park, nothing to send");
  await tap(sb, 0);
  assert(!lastPost(sb, "/api/logging"), "and nothing to restore");
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok " + name); }
    catch (e) { failed++; console.error("FAIL " + name + "\n  " + (e && e.stack || e)); }
  }
  process.exit(failed ? 1 : 0);
})();
