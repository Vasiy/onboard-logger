"use strict";
// Headless harness for the clock-source block in Config -> System.
//
// The block is hidden unless the board found a battery-backed module, because
// every board has *an* RTC: this one answers with two, and the PMIC's own has
// no battery and comes up in 2016. Offering the raw list would be offering a
// choice between a clock and a liar, so app/web/rtc.py decides and this only
// draws the answer -- naming what it picked, since "RTC detected" would be just
// as true of the wrong one.
//
// The standing hint says the board has no battery-backed clock. Once it has
// one, that sentence is wrong, so it is swapped rather than left to contradict
// the switch sitting above it.
//
// Run directly: node tests/ui_rtc.js  (also driven by test_ui_rtc.py)

const { makeSandbox } = require("./ui_sandbox");

let failed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }

// what this NanoPi NEO3 answered on 2026-09-19
const FOUND = {
  devices: [
    { dev: "rtc0", name: "rk808-rtc", bus: "i2c-1", external: false, onboard: true },
    { dev: "rtc1", name: "rtc-ds1307", bus: "i2c-0", external: true, onboard: false },
  ],
  selected: "rtc1", name: "rtc-ds1307", bus: "i2c-0",
  trusted: true, source: "web", available: true,
};
const NONE = { devices: [], selected: "", name: "", bus: "", trusted: false,
               source: "web", available: false };

function harness(status) {
  const calls = [];
  const reply = (body) => Promise.resolve({
    ok: true, status: 200, json: () => Promise.resolve(body),
  });
  const sb = makeSandbox({
    fetch: (path, opts) => {
      calls.push({ path, opts });
      if (path === "/api/system/rtc") return reply(status);
      if (path === "/api/config") return reply({ locale: "en", system: {} });
      return reply({ ok: true });
    },
  });
  return { sb, calls };
}

test("a board with a real module shows the switch", async () => {
  const { sb, calls } = harness(FOUND);
  await sb.loadRtc();
  assert(calls.some((c) => c.path === "/api/system/rtc"), "the board is asked, not guessed");
  assert(sb.document.querySelector("#rtcBox").hidden === false, "the switch belongs on this board");
});

test("with nothing but the SoC clock the block stays hidden", async () => {
  const { sb } = harness(NONE);
  await sb.loadRtc();
  assert(sb.document.querySelector("#rtcBox").hidden === true,
         "offering the choice here would be offering the liar");
});

// The sandbox never parses index.html, so the two static <button>s that live
// under #rtcSource in the real page do not exist here on their own -- built
// once, the same way ui_presets.js builds the rows a real page would already
// have, so loadRtc()'s [...box.children] has something to light.
function seg(sb) {
  const box = sb.document.querySelector("#rtcSource");
  for (const src of ["web", "rtc"]) {
    const b = sb.document.createElement("button");
    b.dataset.src = src;
    box.appendChild(b);
  }
  return box;
}

test("the seg control lights the switch's actual state, not a guess", async () => {
  const { sb } = harness({ ...FOUND, source: "rtc" });
  const box = seg(sb);
  await sb.loadRtc();
  assert(box.children.some((b) => b.dataset.src === "web" && !b.classList.contains("on")) &&
         box.children.some((b) => b.dataset.src === "rtc" && b.classList.contains("on")),
         "exactly the board's own source is lit: " + box.children.map((b) => b.dataset.src + "=" + b.classList.contains("on")).join(", "));
});

test("picking the other state posts the nested config key and repaints", async () => {
  const { sb, calls } = harness(FOUND);   // source: "web"
  await sb.loadRtc();
  // #rtcSource has one delegated click listener, like the log-kind filter;
  // the fake target only needs closest() and dataset, exactly as
  // ui_board_logs.js drives #boardLogFilter
  const btn = { closest: () => btn, dataset: { src: "rtc" }, classList: { contains: () => false } };
  const listener = sb.document.querySelector("#rtcSource").handlers.click[0];
  await listener({ target: btn });
  const posts = calls.filter((c) => c.path === "/api/config" && c.opts && c.opts.method === "POST");
  assert(posts.length === 1, "one POST, got " + posts.length);
  const body = JSON.parse(posts[0].opts.body);
  assert(body.system.rtc.source === "rtc", "wrong payload: " + posts[0].opts.body);
  // loadRtc() runs again after the POST and repaints from the board's answer,
  // which this harness still serves as "web" -- a real board would answer "rtc"
  // and the button would follow; the point here is only that it asks again
  assert(calls.filter((c) => c.path === "/api/system/rtc").length === 2,
         "the switch re-reads the board rather than assuming the click stuck");
});

test("the standing hint stops claiming there is no battery-backed clock", async () => {
  const withRtc = harness(FOUND);
  await withRtc.sb.loadRtc();
  const a = withRtc.sb.document.querySelector("#autoTimeHint").textContent;
  assert(/battery-backed clock\./.test(a) && !/no battery-backed/.test(a),
         "a board with an RTC must not be told it has none: " + a);

  const without = harness(NONE);
  await without.sb.loadRtc();
  const b = without.sb.document.querySelector("#autoTimeHint").textContent;
  assert(/no battery-backed clock/.test(b), "and the old board keeps the old sentence: " + b);
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok " + name); }
    catch (e) { failed++; console.error("FAIL " + name + "\n  " + e.message); }
  }
  process.exit(failed ? 1 : 0);
})();
