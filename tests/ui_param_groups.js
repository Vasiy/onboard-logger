"use strict";
// Headless harness for the three-way split of the parameter list.
//
// The list used to be two piles keyed off one flag: `default` meant both "this is
// a named channel" and "a fresh device polls it". Naming the channels broke
// that: a channel can now be named and still unproven
// on the bike. So the board sends `group` (known | check | unknown) and the list
// draws three sections, while `default` keeps its own job.
//
// app/static/app.js is evaluated whole in a vm on the shared DOM stub and the
// list is driven through applySnapshot(), exactly as the websocket does.
//
// Run directly: node tests/ui_param_groups.js  (also driven by test_ui_param_groups.py)

const { makeSandbox } = require("./ui_sandbox");

const CAT = [
  { key: "rpm", name: "RPM", unit: "rpm", rli: 48, default: true, group: "known", map: "", map_type: "" },
  { key: "vbat", name: "Voltage", unit: "V", rli: 60, default: true, group: "known", map: "", map_type: "" },
  { key: "side_stand", name: "Status side stand", unit: "", rli: 118, default: false,
    group: "check", map: "sidestand", map_type: "enum" },
  { key: "clutch", name: "Status clutch", unit: "", rli: 120, default: false,
    group: "check", map: "clutch", map_type: "enum" },
  { key: "inj_period", name: "Injection period", unit: "", rli: 57, default: true,
    group: "check", map: "", map_type: "" },
  { key: "r31", name: "rli 0x31", unit: "", rli: 49, default: false, group: "unknown", map: "", map_type: "" },
  { key: "r36", name: "rli 0x36", unit: "", rli: 54, default: false, group: "unknown", map: "", map_type: "" },
];

const SNAP = (catalog, selected) => ({
  status: "connected", status_msg: "", ecu_hw: "IAW5AMHW610", ecu_id: "IAW5AM", ecu_desc: "IAW 5AM",
  ecu_fields: {}, poll_hz: 1.2, bus_baud: 10400,
  logging_decoded: false, logging_raw: false,
  log_decoded_file: "", log_decoded_records: 0, log_raw_file: "", log_raw_records: 0,
  storage_root: "/root/k-line", storage_dest: "internal", storage_fallback: false,
  scan_on: false, scan_total: 0, scan_pos: 0, scan_alive: 0, scan_remaining: -1, scan_sweeps: 0,
  wifi_mode: "ap", wifi_link: {}, ap_channel: 6,
  test_mode: false, test_mode_detail: "", act_key: "", act_lid: 0, act_until: 0,
  values: {}, values_ts: 0, catalog, selected,
});

let failed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }

// applySnapshot() walks from the two log switches up to their .rec-row, which the
// stub only knows about once the row exists — build it before the first snapshot.
function sandbox() {
  const sb = makeSandbox();
  for (const id of ["#decToggle", "#rawToggle"]) {
    const row = sb.document.createElement("div");
    row.classList.add("rec-row");
    row.appendChild(sb.document.querySelector(id));
  }
  return sb;
}

const rows = (sb) => sb.document.querySelector("#params").children;
const keysOf = (list) => list.filter((n) => n.dataset.key).map((n) => n.dataset.key);

test("known channels come first, unproven ones follow under their own label", () => {
  const sb = sandbox();
  sb.applySnapshot(SNAP(CAT, ["rpm"]));
  const kids = rows(sb);
  const label = kids.find((n) => n.classList.contains("params-group"));
  assert(label, "the check group needs a label of its own");
  const before = keysOf(kids.slice(0, kids.indexOf(label)));
  const after = keysOf(kids.slice(kids.indexOf(label)));
  assert(before.join(",") === "rpm,vbat", "known first, got " + before.join(","));
  assert(after.join(",") === "side_stand,clutch,inj_period", "check after the label, got " + after.join(","));
  assert(label.textContent.includes(sb.window.I18N.en["params.check"]),
         "the label should be the translated one, got " + label.textContent);
  assert(label.textContent.includes("(3)"), "the label carries the count, got " + label.textContent);
  assert(label.title === sb.window.I18N.en["params.checkHint"], "the label explains itself on hover");
});

test("a channel to confirm is visible without expanding anything", () => {
  const sb = sandbox();
  sb.applySnapshot(SNAP(CAT, []));
  const kids = rows(sb);
  const wrap = kids.find((n) => n.classList.contains("unknown-wrap"));
  assert(wrap, "the unidentified rli still collapse");
  assert(wrap.hidden === true, "with nothing ticked the unknown block starts folded");
  assert(keysOf(wrap.children).join(",") === "r31,r36", "only unknown rli go inside the fold");
  const outside = keysOf(kids);
  assert(outside.includes("side_stand") && outside.includes("clutch"),
         "the side stand and the clutch must not be hidden in the fold");
});

test("the fold still opens by itself when something inside it is ticked", () => {
  const sb = sandbox();
  sb.applySnapshot(SNAP(CAT, ["r36"]));
  const wrap = rows(sb).find((n) => n.classList.contains("unknown-wrap"));
  assert(wrap.hidden === false, "a ticked unknown rli opens the fold");
});

test("a board that predates the split still renders in two piles", () => {
  const sb = sandbox();
  const old = CAT.map(({ group, ...rest }) => rest);       // no `group` on the wire
  sb.applySnapshot(SNAP(old, []));
  const kids = rows(sb);
  assert(!kids.some((n) => n.classList.contains("params-group")),
         "without `group` there is nothing to label");
  const wrap = kids.find((n) => n.classList.contains("unknown-wrap"));
  assert(keysOf(wrap.children).join(",") === "side_stand,clutch,r31,r36",
         "the old rule was `default`, got " + keysOf(wrap.children).join(","));
  assert(keysOf(kids.slice(0, kids.indexOf(wrap))).join(",") === "rpm,vbat,inj_period",
         "default=true stays the top list on an old board");
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok " + name); }
    catch (e) { failed++; console.error("FAIL " + name + "\n  " + e.message); }
  }
  process.exit(failed ? 1 : 0);
})();
