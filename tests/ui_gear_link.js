"use strict";
// Headless harness for the derived channel that follows its inputs.
//
// Gear is not asked for: the board works it out of rpm and road speed (plus the
// neutral and clutch flags, which only keep an upshift from flashing a gear that
// is not there), so its rli never goes on the wire. That is why the tick is
// coupled instead of free -- selecting what it needs switches it on for nothing,
// and losing one of those channels switches it off rather than logging a column
// of "--". Unticking it by hand is still a real choice.
//
// app/static/app.js is evaluated whole in a vm on the shared DOM stub. Rows are
// built with innerHTML, so their checkbox is the stub's per-selector node: the
// harness sets `.checked` on it exactly as a tap does.
//
// Run directly: node tests/ui_gear_link.js   (also driven by test_ui_gear_link.py)

const { makeSandbox: baseSandbox } = require("./ui_sandbox");

const CAT = [
  { key: "rpm", name: "RPM", unit: "rpm", rli: 48, default: true, group: "known", map: "", map_type: "" },
  { key: "speed", name: "Road speed", unit: "km/h", rli: 83, default: false, group: "known",
    map: "", map_type: "" },
  { key: "neutral", name: "Status neutral", unit: "", rli: 97, default: false, group: "known",
    map: "neutral", map_type: "enum" },
  { key: "clutch", name: "Status clutch", unit: "", rli: 120, default: false, group: "known",
    map: "clutch", map_type: "enum" },
  { key: "vbat", name: "Voltage", unit: "V", rli: 60, default: true, group: "known", map: "", map_type: "" },
  // rli 0 and never requested: the board derives it from the four above
  { key: "gear", name: "Gear", unit: "", rli: 0, default: false, group: "known",
    map: "", map_type: "", derived: "gear" },
];

const SNAP = (selected, over = {}) => ({
  status: "connected", status_msg: "", ecu_hw: "IAW5AMHW610", ecu_id: "IAW5AM", ecu_desc: "IAW 5AM",
  ecu_fields: {}, poll_hz: 1.2, bus_baud: 10400,
  logging_decoded: false, logging_raw: false,
  log_decoded_file: "", log_decoded_records: 0, log_raw_file: "", log_raw_records: 0,
  storage_root: "/root/k-line", storage_dest: "internal", storage_fallback: false,
  scan_on: false, scan_total: 0, scan_pos: 0, scan_alive: 0, scan_remaining: -1, scan_sweeps: 0,
  wifi_mode: "ap", wifi_link: {}, ap_channel: 6,
  test_mode: false, test_mode_detail: "", act_key: "", act_lid: 0, act_until: 0,
  poll_req_ms: 0, poll_min_ms: 150,
  values: {}, values_ts: 0, catalog: CAT, selected,
  presets: [{ name: "", keys: [] }, { name: "", keys: [] }, { name: "", keys: [] }], ...over,
});

const drain = async () => { for (let i = 0; i < 8; i++) await new Promise(setImmediate); };

async function sandbox(mode = "edit") {
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
  posts.length = 0;
  sb.posts = posts;
  return sb;
}

let failed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }

const rowsOf = (sb) => sb.document.querySelector("#params").querySelectorAll(".prow");
const rowFor = (sb, key) => rowsOf(sb).find((r) => r.dataset.key === key);
const boxFor = (sb, key) => rowFor(sb, key).querySelector("input");
const lastPost = (sb, url) => [...sb.posts].reverse().find(([u]) => u === url);
const sent = (sb) => (lastPost(sb, "/api/selected") || [, { keys: [] }])[1].keys;

// The stub does not parse the row's innerHTML, so a checkbox starts with no
// `checked` at all: seeding them from the snapshot's selection is what the
// browser's own render does for free.
const seed = (sb, keys) =>
  rowsOf(sb).forEach((r) => (r.querySelector("input").checked = keys.includes(r.dataset.key)));

// a tap on a checkbox: the browser flips it, then fires change
async function tick(sb, key, on) {
  const inp = boxFor(sb, key);
  inp.checked = on;
  await inp.handlers.change[0]({ target: inp });
}

test("completing the set gear is derived from ticks gear by itself", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP(["rpm"]));
  seed(sb, ["rpm"]);
  assert(boxFor(sb, "gear").checked === false, "nothing to derive from yet");
  await tick(sb, "speed", true);
  assert(boxFor(sb, "gear").checked === true, "rpm + speed is all it needs");
  assert(sent(sb).includes("gear"), "and the board is told, got " + sent(sb).join(","));
});

test("ticking gear pulls in the channels it is worked out from", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP([]));
  seed(sb, []);
  await tick(sb, "gear", true);
  const keys = sent(sb);
  for (const k of ["rpm", "speed", "neutral", "clutch", "gear"]) {
    assert(keys.includes(k), k + " should come with gear, got " + keys.join(","));
  }
  assert(!keys.includes("vbat"), "and nothing else does, got " + keys.join(","));
  for (const k of ["rpm", "speed", "neutral", "clutch"]) {
    assert(!rowFor(sb, k).classList.contains("off"), k + "'s row should not stay dimmed");
  }
});

test("losing one of the two it needs puts gear out", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP(["rpm", "speed", "neutral", "clutch", "gear"]));
  seed(sb, ["rpm", "speed", "neutral", "clutch", "gear"]);
  await tick(sb, "speed", false);
  assert(boxFor(sb, "gear").checked === false, "no speed, no ratio, no gear");
  assert(!sent(sb).includes("gear"), "and it is gone from the selection, got " + sent(sb).join(","));
});

test("dropping a flag it only wants keeps gear on", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP(["rpm", "speed", "neutral", "clutch", "gear"]));
  seed(sb, ["rpm", "speed", "neutral", "clutch", "gear"]);
  await tick(sb, "clutch", false);
  assert(boxFor(sb, "gear").checked === true,
         "the clutch flag is a quality fix, not a dependency");
  assert(sent(sb).includes("gear"), "got " + sent(sb).join(","));
});

test("unticking gear by hand sticks, and leaves its inputs alone", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP(["rpm", "speed", "neutral", "clutch", "gear"]));
  seed(sb, ["rpm", "speed", "neutral", "clutch", "gear"]);
  await tick(sb, "gear", false);
  assert(boxFor(sb, "gear").checked === false, "an explicit tick is a real choice");
  const keys = sent(sb);
  assert(!keys.includes("gear"), "got " + keys.join(","));
  for (const k of ["rpm", "speed", "neutral", "clutch"]) {
    assert(keys.includes(k), k + " was not asked to go, got " + keys.join(","));
  }
  // and a change somewhere else must not quietly bring it back
  await tick(sb, "vbat", true);
  assert(boxFor(sb, "gear").checked === false, "an unrelated tick is not a reason to re-tick gear");
});

test("none clears gear too, all leaves it on", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP(["rpm", "speed", "gear"]));
  seed(sb, ["rpm", "speed", "gear"]);
  const none = sb.document.querySelector("#selNone");
  await none.handlers.click[0]({});
  await drain();
  assert(boxFor(sb, "gear").checked === false, "with nothing selected there is nothing to derive");
  const all = sb.document.querySelector("#selAll");
  await all.handlers.click[0]({});
  await drain();
  assert(boxFor(sb, "gear").checked === true, "everything selected includes what it needs");
});

test("gear is free: it does not price as a request", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP(["rpm"]));
  seed(sb, ["rpm"]);
  await tick(sb, "speed", true);
  const cost = () => sb.document.querySelector("#presetCost").textContent;
  const before = cost();
  assert(boxFor(sb, "gear").checked === true, "coupled on by rpm + speed");
  assert(before.includes("2 "), "two requests on the wire, got " + before);
  await tick(sb, "gear", false);
  assert(cost() === before, "dropping a derived channel changes nothing, got " + cost());
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok " + name); }
    catch (e) { failed++; console.error("FAIL " + name + "\n  " + (e && e.stack || e)); }
  }
  process.exit(failed ? 1 : 0);
})();
