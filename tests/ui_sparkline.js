"use strict";
// Headless harness for the fixed sparkline window.
//
// A coolant sensor drifting by tenths used to fill the whole height, because
// the sparkline auto-scaled to whatever the last three seconds happened to
// hold: the line said "something is happening" when nothing was. The two
// temperature channels get a window of +-3 C centred on the newest reading, so
// the scale never changes and a warm-up slides through the window instead of
// climbing off the top.
//
// The window only ever widens. A temperature that really leaves it inside the
// trace is the one thing worth seeing, and clipping would hide exactly that.
//
// How long that trace is is board config (ui.spark_s, 3..30 s), so the second
// half of this harness pins the buffer's trimming and the x-scale against it.
//
// Snapshots go in through applySnapshot(), the way they arrive over the
// WebSocket, so the catalog and the sparkline buffer are filled by the same
// code the browser runs rather than by reaching into either.
//
// Run directly: node tests/ui_sparkline.js  (also driven by test_ui_sparkline.py)

const { makeSandbox: baseSandbox } = require("./ui_sandbox");

const CATALOG = [
  { key: "coolant_t", name: "Engine temperature", unit: "°C", group: "known" },
  { key: "air_t", name: "Air temperature", unit: "°C", group: "known" },
  { key: "rpm", name: "RPM", unit: "rpm", group: "known" },
];

function snap(values) {
  return {
    status: "connected", values: values, catalog: CATALOG,
    selected: ["coolant_t", "air_t", "rpm"],
    log_decoded_file: "", log_raw_file: "", presets: null,
    storage: {}, wifi: {}, dtc: [],
  };
}

function makeSandbox(cfg) {
  const sb = baseSandbox({
    fetch(url) {
      // loadConfig() reads the Wi-Fi block unconditionally, so the stub answer
      // carries one even for the tests that only care about the tile window
      const base = { logging: {}, diag: {}, dhcp: {}, network: {},
                     wifi: { mode: "ap", auto_channel: true, channel: 6, client: { prefix: 24 } } };
      const body = url === "/api/config" ? Object.assign(base, cfg)
        : url === "/api/addons" ? { addons: [] }
        : { files: [] };
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
    },
  });
  // applySnapshot() locks the log switches through closest(".rec-row"); on the
  // stub every node hangs off body, so that ancestor has to be built here
  for (const sel of ["#decToggle", "#rawToggle"]) {
    const row = sb.document.createElement("div");
    row.className = "rec-row";
    row.appendChild(sb.document.querySelector(sel));
  }
  return sb;
}

let failed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }
function eq(got, want, msg) {
  assert(String(got) === String(want),
    (msg || "mismatch") + `: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}

const feed = (sb, values) => sb.window.applySnapshot(snap(values));
const span = (sb, key) => sb.window.fixedSpan(key);

/* A canvas that records where the line was actually put. Height 20, pad 2, so
   the plotted band is 16 px: y=18 is its bottom and y=2 its top. Width 100 with
   the same padding puts the window's start at x=2 and its newest point at 98. */
function fakeCanvas() {
  const ys = [], xs = [];
  return {
    ys, xs,
    clientWidth: 100, clientHeight: 20, width: 0, height: 0,
    getContext: () => ({
      setTransform() {}, clearRect() {}, beginPath() {}, stroke() {},
      moveTo(x, y) { xs.push(x); ys.push(y); }, lineTo(x, y) { xs.push(x); ys.push(y); },
      strokeStyle: "", globalAlpha: 1, lineWidth: 1,
    }),
  };
}

const draw = (sb, hist, sp) => {
  const cv = fakeCanvas();
  sb.window.drawSpark(cv, hist, false, sp);
  return cv.ys;
};

const drawXs = (sb, hist, sp) => {
  const cv = fakeCanvas();
  sb.window.drawSpark(cv, hist, false, sp);
  return cv.xs;
};

// the sandbox freezes performance.now(); the buffer is trimmed against it, so a
// harness that wants to age a sample moves the clock itself
const at = (sb, ms) => { sb.performance.now = () => ms; };

test("the window is six degrees wide, centred on the reading", () => {
  const sb = makeSandbox();
  feed(sb, { coolant_t: 81.4, air_t: 22.0 });
  const c = span(sb, "coolant_t");
  eq(c.mn.toFixed(1), "78.4");
  eq(c.mx.toFixed(1), "84.4");
  eq((c.mx - c.mn).toFixed(1), "6.0", "±3 °C");
  const a = span(sb, "air_t");
  eq(a.mn.toFixed(1), "19.0");
  eq(a.mx.toFixed(1), "25.0");
});

test("the window follows the temperature rather than staying where it started", () => {
  const sb = makeSandbox();
  feed(sb, { coolant_t: 20 });
  eq(span(sb, "coolant_t").mn, 17);
  for (const v of [45, 70, 88]) feed(sb, { coolant_t: v });
  eq(span(sb, "coolant_t").mn, 85, "centred on the newest reading");
  eq(span(sb, "coolant_t").mx, 91);
});

test("a channel with no fixed window keeps auto-scaling", () => {
  const sb = makeSandbox();
  feed(sb, { rpm: 3000, coolant_t: 80 });
  eq(span(sb, "rpm"), "null", "rpm has no fixed window");
});

test("nothing is centred until a real number arrives", () => {
  const sb = makeSandbox();
  eq(span(sb, "coolant_t"), "null", "no readings yet");
  feed(sb, { coolant_t: null, air_t: undefined });
  eq(span(sb, "coolant_t"), "null");
  feed(sb, { coolant_t: NaN });
  eq(span(sb, "coolant_t"), "null", "NaN is not a reading");
  feed(sb, { coolant_t: 70 });
  eq(span(sb, "coolant_t").mn, 67);
});

test("a gap in the readings leaves the last good one in charge", () => {
  // the buffer only ever takes finite values, so a dropped sample cannot
  // blank the window mid-ride
  const sb = makeSandbox();
  feed(sb, { coolant_t: 90 });
  feed(sb, { coolant_t: null });
  eq(span(sb, "coolant_t").mn, 87, "still centred on the last real reading");
});

test("without a window, tenths of drift fill the whole height", () => {
  // the behaviour being fixed, pinned so it cannot come back by accident
  const sb = makeSandbox();
  const ys = draw(sb, [[0, 80.0], [1, 79.9]], null);
  eq(ys.length, 2);
  eq(Math.min(...ys), 2, "0.1 °C reached the top of the box");
  eq(Math.max(...ys), 18, "...and the bottom");
});

test("inside the window, the same drift is nearly a flat line", () => {
  const sb = makeSandbox();
  feed(sb, { coolant_t: 80 });
  const ys = draw(sb, [[0, 80.0], [1, 79.9]], span(sb, "coolant_t"));
  eq(ys.length, 2);
  eq(ys[0].toFixed(2), "10.00", "80 °C sits mid-height, at the centre");
  const swing = Math.max(...ys) - Math.min(...ys);
  assert(swing < 1, "0.1 °C out of 6 should move under a pixel, moved " + swing);
});

test("a swing wider than the window is not clipped", () => {
  const sb = makeSandbox();
  feed(sb, { coolant_t: 80 });
  // 77..83 is the window; a trace reaching 92 within the three seconds is real
  const ys = draw(sb, [[0, 92], [1, 80]], span(sb, "coolant_t"));
  eq(ys.length, 2);
  eq(Math.min(...ys), 2, "the excursion reaches the top rather than being cut off");
  // the window's floor still anchors the bottom: 77..92 is 15 wide, 80 is 3 up
  eq(ys[1].toFixed(2), (18 - (3 / 15) * 16).toFixed(2), "80 °C keeps its place");
});

test("a steady warm-up keeps the trace on screen instead of ramping off it", () => {
  // the point of centring on the newest value: three seconds of climb sits
  // inside a window that moved with it, rather than stretching the range
  const sb = makeSandbox();
  feed(sb, { coolant_t: 60 });
  const ys = draw(sb, [[0, 59.4], [1, 59.7], [2, 60]], span(sb, "coolant_t"));
  eq(ys.length, 3);
  assert(Math.min(...ys) > 2, "nothing is pinned to the top: " + ys.join(","));
  assert(Math.max(...ys) < 18, "nor to the bottom: " + ys.join(","));
});

/* ---------- the history window is configurable (ui.spark_s, 3..30 s) ---------- */

test("the window defaults to three seconds and clamps to the slider's range", () => {
  const sb = makeSandbox();
  eq(sb.window.sparkSpanMs(), 3000, "default");
  eq(sb.window.setSparkSpan(10), 10000);
  eq(sb.window.setSparkSpan(30), 30000);
  eq(sb.window.setSparkSpan(31), 30000, "above the maximum");
  eq(sb.window.setSparkSpan(2), 3000, "below the minimum");
  eq(sb.window.setSparkSpan(7.4), 7000, "whole seconds");
  eq(sb.window.setSparkSpan("soon"), 7000, "junk leaves the window alone");
});

test("the board's value is what the window ends up being", async () => {
  const sb = makeSandbox({ ui: { spark_s: 12 } });
  await sb.window.loadConfig();
  eq(sb.window.sparkSpanMs(), 12000);
});

test("a board that sends no ui section keeps the three-second window", async () => {
  const sb = makeSandbox();
  await sb.window.loadConfig();
  eq(sb.window.sparkSpanMs(), 3000);
});

test("the buffer keeps exactly the configured window", () => {
  const sb = makeSandbox();
  sb.window.setSparkSpan(10);
  at(sb, 0);      feed(sb, { rpm: 1000 });
  at(sb, 8000);   feed(sb, { rpm: 2000 });
  eq(sb.window.sparkHist("rpm").length, 2, "8 s back is inside a 10 s window");
  at(sb, 11000);  feed(sb, { rpm: 3000 });
  eq(sb.window.sparkHist("rpm").length, 2, "the first sample has aged out");
  eq(sb.window.sparkHist("rpm")[0][1], 2000);
  // shortening the window drops what no longer fits at the next sample
  sb.window.setSparkSpan(3);
  at(sb, 12000);  feed(sb, { rpm: 4000 });
  eq(sb.window.sparkHist("rpm").length, 2, "only 11 s and 12 s survive a 3 s window");
  eq(sb.window.sparkHist("rpm")[0][1], 3000);
});

test("the x axis is the window, so the same trace stretches when it widens", () => {
  const sb = makeSandbox();
  // a sample five seconds old: outside a 3 s window, mid-way through a 10 s one
  const hist = [[0, 10], [5000, 20]];
  sb.window.setSparkSpan(10);
  const wide = drawXs(sb, hist, null);
  eq(wide[0].toFixed(2), "50.00", "5 s back is half-way across a 10 s window");
  eq(wide[1].toFixed(2), "98.00", "the newest sample is always at the right edge");
  sb.window.setSparkSpan(3);
  const narrow = drawXs(sb, hist, null);
  eq(narrow[1].toFixed(2), "98.00");
  assert(narrow[0] < 0, "the same sample falls off the left of a 3 s window: " + narrow[0]);
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok " + name); }
    catch (e) { failed++; console.log("FAIL " + name + "\n   " + e.message); }
  }
  console.log(`\n${tests.length - failed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
