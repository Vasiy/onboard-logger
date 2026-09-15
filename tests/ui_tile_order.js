"use strict";
// Headless harness for the tile order on the Logger tab.
//
// Read mode lays the picked channels out as big tiles; the rider presses and
// holds one, the grid starts wiggling, and the tiles are dragged into whatever
// order suits the bike. The order belongs to the *preset*, like its note, and
// rides on the board so any phone that joins the AP sees the same layout.
//
// The DOM stub has no pointer events, no dispatchEvent and no
// getBoundingClientRect, so the seam runs between the pure half — which order do
// these keys end up in, which tile did the finger land on — and a thin event
// layer that only calls into it. Everything below drives the pure half plus the
// handlers the bar's two buttons carry, exactly as ui_presets.js does.
//
// Run directly: node tests/ui_tile_order.js  (also driven by test_ui_tile_order.py)

const { makeSandbox: baseSandbox } = require("./ui_sandbox");

const CAT = [
  { key: "rpm", name: "RPM", unit: "rpm", rli: 48, default: true, group: "known", map: "", map_type: "" },
  { key: "vbat", name: "Voltage", unit: "V", rli: 60, default: true, group: "known", map: "", map_type: "" },
  { key: "tmot", name: "Coolant", unit: "C", rli: 49, default: true, group: "known", map: "", map_type: "" },
  { key: "lambda", name: "Lambda", unit: "", rli: 80, default: false, group: "check", map: "", map_type: "" },
  { key: "r36", name: "rli 0x36", unit: "", rli: 54, default: false, group: "unknown", map: "", map_type: "" },
];

const PRESETS = (over = []) => [
  { name: "Dyno", keys: ["rpm", "tmot", "vbat"], note: "", order: [], ...(over[0] || {}) },
  { name: "Cold", keys: ["rpm", "lambda"], note: "", order: [], ...(over[1] || {}) },
  { name: "", keys: [], note: "", order: [], ...(over[2] || {}) },
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
  poll_req_ms: 0, poll_min_ms: 150,
  values: {}, values_ts: 0, catalog: CAT,
  selected: ["rpm", "tmot", "vbat"], presets: PRESETS(), free_note: "", free_order: [], ...over,
});

const drain = async () => { for (let i = 0; i < 8; i++) await new Promise(setImmediate); };

async function sandbox(mode = "view") {
  const posts = [];
  const sb = baseSandbox({
    storage: { paramMode: mode },
    fetch(url, opts) {
      const body = opts && opts.body ? JSON.parse(opts.body) : null;
      if (opts && opts.method === "POST") posts.push([url, body]);
      // the board echoes a posted preset list back the way /api/presets does
      const out = url === "/api/presets"
        ? { presets: body.presets, free_note: body.free_note, free_order: body.free_order }
        : {};
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
const ordOf = (sb, key) => rowFor(sb, key).style.getPropertyValue("--ord");
const lastPost = (sb, url) => [...sb.posts].reverse().find(([u]) => u === url);
const postsTo = (sb, url) => sb.posts.filter(([u]) => u === url);
const CATKEYS = CAT.map((c) => c.key);

// the tiles on screen, newest layout first, read back off the grid the way the
// eye reads them: by --ord, not by DOM position
function laidOut(sb) {
  return rowsOf(sb)
    .filter((r) => !r.classList.contains("off"))
    .sort((a, b) => Number(a.style.getPropertyValue("--ord")) -
                    Number(b.style.getPropertyValue("--ord")))
    .map((r) => r.dataset.key);
}

// Rows are built with innerHTML, which the DOM stub does not parse, so the
// `checked` attribute in that string never reaches the stub's checkbox. A test
// that ticks one channel has to put the rest where the snapshot says they are
// first, or the change handler reads every other row as unticked.
function seedTicks(sb, selected) {
  const want = new Set(selected);
  rowsOf(sb).forEach((r) => (r.querySelector("input").checked = want.has(r.dataset.key)));
}

async function tick(sb, key, on) {
  const inp = rowFor(sb, key).querySelector("input");
  inp.checked = on;
  await inp.handlers.change[0]({ target: inp });
}

// ---------- the pure half ----------

test("with nothing stored the tiles sit in catalog order", async () => {
  const sb = await sandbox();
  const got = sb.window.resolveTileOrder([], ["tmot", "rpm"], CAT);
  assert(got.join(",") === "rpm,tmot", "got " + got.join(","));
});

test("a stored order comes first, and the rest follow in catalog order", async () => {
  const sb = await sandbox();
  const got = sb.window.resolveTileOrder(["tmot"], ["rpm", "tmot", "vbat"], CAT);
  assert(got.join(",") === "tmot,rpm,vbat", "got " + got.join(","));
});

test("a stored key that is no longer selected is skipped, not rendered", async () => {
  const sb = await sandbox();
  // `vbat` was arranged once and has since been unticked; `lambda` is new
  const got = sb.window.resolveTileOrder(["vbat", "tmot"], ["tmot", "lambda"], CAT);
  assert(got.join(",") === "tmot,lambda", "got " + got.join(","));
});

test("a channel ticked after the arrangement lands last, never first", async () => {
  const sb = await sandbox();
  const got = sb.window.resolveTileOrder(["tmot", "rpm"], ["rpm", "tmot", "lambda"], CAT);
  assert(got.join(",") === "tmot,rpm,lambda", "got " + got.join(","));
});

test("moveItem moves to the front, to the end and onto itself", async () => {
  const sb = await sandbox();
  const m = sb.window.moveItem;
  assert(m(["a", "b", "c"], 2, 0).join("") === "cab", "to the front");
  assert(m(["a", "b", "c"], 0, 2).join("") === "bca", "to the end");
  assert(m(["a", "b", "c"], 1, 1).join("") === "abc", "onto itself changes nothing");
  assert(m(["a", "b", "c"], 0, 9).join("") === "bca", "past the end clamps");
  assert(m(["a", "b", "c"], 0, -3).join("") === "abc", "before the start clamps");
});

test("dropIndex names the tile the finger is over, wrapped grid included", async () => {
  const sb = await sandbox();
  // a 3x2 grid of 100x60 tiles: 0 1 2 on the first row, 3 4 on the second
  const rects = [0, 1, 2, 3, 4].map((i) => {
    const col = i % 3, row = Math.floor(i / 3);
    return { left: col * 100, right: col * 100 + 100, top: row * 60, bottom: row * 60 + 60 };
  });
  const d = sb.window.dropIndex;
  assert(d(rects, 250, 30, 0) === 2, "over the third tile, got " + d(rects, 250, 30, 0));
  assert(d(rects, 50, 90, 2) === 3, "dropped onto the second row, got " + d(rects, 50, 90, 2));
  // past the last tile on a short final row: the nearest centre is still the last
  assert(d(rects, 290, 90, 0) === 4, "past the end, got " + d(rects, 290, 90, 0));
});

test("merging keeps an unticked channel anchored where it was", async () => {
  const sb = await sandbox();
  // vbat is arranged between rpm and tmot, then unticked: the grid can only show
  // the two that are left, so the drag produces a dense list without it
  const stored = ["rpm", "vbat", "tmot"];
  const got = sb.window.mergeTileOrder(stored, ["tmot", "rpm"]);
  assert(got.join(",") === "tmot,vbat,rpm", "got " + got.join(","));
  // ticking it back on therefore puts it back between them, not at the end
  assert(sb.window.resolveTileOrder(got, ["rpm", "vbat", "tmot"], CAT).join(",")
         === "tmot,vbat,rpm", "and it comes back where it was");
});

test("merging appends a key the stored order never knew", async () => {
  const sb = await sandbox();
  const got = sb.window.mergeTileOrder(["rpm"], ["lambda", "rpm"]);
  assert(got.join(",") === "lambda,rpm", "got " + got.join(","));
  assert(sb.window.mergeTileOrder([], ["rpm", "tmot"]).join(",") === "rpm,tmot",
         "nothing stored: the dense list is the whole answer");
});

// ---------- applying it ----------

test("every tile is numbered, so none can float to the front", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP());
  assert(laidOut(sb).join(",") === "rpm,vbat,tmot", "catalog order first, got " + laidOut(sb));
  for (const r of rowsOf(sb)) {
    assert(r.style.getPropertyValue("--ord") !== "",
           "row " + r.dataset.key + " was left at the default");
  }
  // the picked ones are 0..n-1 with no gaps; the rest are parked out of the way
  const on = rowsOf(sb).filter((r) => !r.classList.contains("off"))
    .map((r) => Number(r.style.getPropertyValue("--ord"))).sort((a, b) => a - b);
  assert(on.join(",") === "0,1,2", "got " + on.join(","));
  assert(Number(ordOf(sb, "lambda")) > 900, "an unpicked row sorts far behind");
});

test("a stored order paints the tiles in it", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP({ presets: PRESETS([{ order: ["tmot", "vbat", "rpm"] }]) }));
  assert(laidOut(sb).join(",") === "tmot,vbat,rpm", "got " + laidOut(sb));
});

test("each preset keeps its own layout", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP({
    presets: PRESETS([{ order: ["tmot", "vbat", "rpm"] }, { order: ["lambda", "rpm"] }]),
  }));
  assert(laidOut(sb).join(",") === "tmot,vbat,rpm", "slot 0, got " + laidOut(sb));
  // switching to the second preset is an ordinary change of the selection
  await sb.window.applyPreset(1);
  assert(laidOut(sb).join(",") === "lambda,rpm", "slot 1, got " + laidOut(sb));
});

test("a hand-picked set that matches no preset keeps the free order", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP({ selected: ["rpm", "vbat"], free_order: ["vbat", "rpm"],
                          presets: PRESETS() }));
  assert(laidOut(sb).join(",") === "vbat,rpm", "got " + laidOut(sb));
});

// ---------- arrange mode ----------

test("arrange mode wiggles the grid and shows its bar", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP());
  const box = sb.document.querySelector("#params");
  const bar = sb.document.querySelector("#arrangeBar");
  assert(bar.hidden === true, "the bar starts hidden");
  sb.window.enterArrange();
  assert(box.classList.contains("params--arrange"), "the grid gets the wiggle class");
  assert(bar.hidden === false, "and the bar appears");
  sb.window.exitArrange(false);
  assert(!box.classList.contains("params--arrange"), "Done stops the wiggle");
  assert(bar.hidden === true, "and puts the bar away");
});

test("a drop repaints at once and Done writes it to the lit preset", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP());
  sb.window.enterArrange();
  sb.window.reorderTiles("tmot", 0);
  assert(laidOut(sb).join(",") === "tmot,rpm,vbat", "the grid follows, got " + laidOut(sb));
  assert(!lastPost(sb, "/api/presets"), "nothing goes out mid-arrangement");
  await sb.window.exitArrange(true);
  const [, body] = lastPost(sb, "/api/presets");
  assert(body.presets[0].order.join(",") === "tmot,rpm,vbat",
         "the lit slot took the order, got " + JSON.stringify(body.presets[0].order));
  assert(body.presets[1].order.length === 0, "and only that slot");
});

test("with no preset lit the order goes to the free one", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP({ selected: ["rpm", "vbat"] }));   // matches no slot
  sb.window.enterArrange();
  sb.window.reorderTiles("vbat", 0);
  await sb.window.exitArrange(true);
  const [, body] = lastPost(sb, "/api/presets");
  assert(body.free_order.join(",") === "vbat,rpm", "got " + JSON.stringify(body.free_order));
  assert(body.presets.every((p) => !p.order.length), "no slot was touched");
});

test("one write per arranging session, not one per drop", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP());
  sb.window.enterArrange();
  sb.window.reorderTiles("tmot", 0);
  sb.window.reorderTiles("vbat", 0);
  sb.window.reorderTiles("rpm", 0);
  await sb.window.exitArrange(true);
  assert(postsTo(sb, "/api/presets").length === 1,
         "got " + postsTo(sb, "/api/presets").length + " writes");
});

test("leaving without moving anything writes nothing", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP());
  sb.window.enterArrange();
  await sb.window.exitArrange(true);
  assert(!lastPost(sb, "/api/presets"), "an untouched arrangement is not a change");
});

test("reset puts the tiles back in catalog order and stores that", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP({ presets: PRESETS([{ order: ["tmot", "vbat", "rpm"] }]) }));
  assert(laidOut(sb).join(",") === "tmot,vbat,rpm", "arranged to start with");
  sb.window.enterArrange();
  await sb.window.resetTileOrder();
  assert(laidOut(sb).join(",") === "rpm,vbat,tmot", "back to catalog order, got " + laidOut(sb));
  const [, body] = lastPost(sb, "/api/presets");
  assert(body.presets[0].order.length === 0, "and the slot was emptied on the board");
});

test("the arrangement survives a tick, because the preset it came from is remembered", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP({ presets: PRESETS([{ order: ["tmot", "vbat", "rpm"] }]) }));
  await sb.window.applyPreset(0);
  await tick(sb, "lambda", true);      // the set no longer equals any slot
  assert(laidOut(sb).join(",") === "tmot,vbat,rpm,lambda",
         "the layout holds and the new tile lands last, got " + laidOut(sb));
});

test("a channel that arrives mid-arrangement joins the working list", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP());
  seedTicks(sb, ["rpm", "tmot", "vbat"]);
  sb.window.enterArrange();
  sb.window.reorderTiles("tmot", 0);
  // a tick is not reachable by tapping a tile any more, but the pick list and a
  // board push both still change the selection under a live arrangement
  await tick(sb, "lambda", true);
  assert(laidOut(sb).join(",") === "tmot,rpm,vbat,lambda",
         "it lands last and the arrangement holds, got " + laidOut(sb));
  await sb.window.exitArrange(true);
  const [, body] = lastPost(sb, "/api/presets");
  // the set now matches no slot, so it is the free order that took it -- and it
  // has to name the new channel, not the list as it stood before the tick
  assert(body.free_order.join(",") === "tmot,rpm,vbat,lambda",
         "got " + JSON.stringify(body.free_order));
});

test("a channel that leaves mid-arrangement drops out of it", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP());
  seedTicks(sb, ["rpm", "tmot", "vbat"]);
  sb.window.enterArrange();
  sb.window.reorderTiles("tmot", 0);
  await tick(sb, "vbat", false);
  assert(laidOut(sb).join(",") === "tmot,rpm", "got " + laidOut(sb));
});

test("tapping a preset while arranging stores the arrangement first", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP({
    presets: PRESETS([{}, { order: ["lambda", "rpm"] }]),
  }));
  sb.window.enterArrange();
  sb.window.reorderTiles("tmot", 0);
  await sb.window.applyPreset(1);          // the lit slot changes underneath it
  const [, body] = lastPost(sb, "/api/presets");
  assert(body.presets[0].order.join(",") === "tmot,rpm,vbat",
         "slot 0 kept what was made on it, got " + JSON.stringify(body.presets[0].order));
  assert(laidOut(sb).join(",") === "lambda,rpm",
         "and slot 1 shows its own layout, got " + laidOut(sb));
});

// ---------- the tap that used to switch a channel off ----------

test("a tap on a tile no longer unticks the channel", async () => {
  const sb = await sandbox();
  sb.applySnapshot(SNAP());
  const row = rowFor(sb, "rpm");
  let prevented = false;
  row.handlers.click[0]({ preventDefault: () => { prevented = true; } });
  assert(prevented, "read mode cancels the label's own activation behaviour");
});

test("in pick mode the label still ticks its checkbox", async () => {
  const sb = await sandbox("edit");
  sb.applySnapshot(SNAP());
  const row = rowFor(sb, "rpm");
  let prevented = false;
  row.handlers.click[0]({ preventDefault: () => { prevented = true; } });
  assert(!prevented, "picking channels is what that list is for");
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok " + name); }
    catch (e) { failed++; console.error("FAIL " + name + "\n  " + (e && e.stack || e)); }
  }
  process.exit(failed ? 1 : 0);
})();
