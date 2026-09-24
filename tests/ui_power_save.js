"use strict";
// Headless harness for the Power save block in Config -> System.
//
// Two things are worth pinning here. The hint counts from the board's own idle
// clock, not from anything the phone knows -- a phone can join the AP long after
// the ignition went off, so a browser-side timer would start at zero and the
// rider would read "0 min" on a board about to power itself off. And the three
// fields go out through the ordinary /api/config autosave: system.* is not a
// NET_FIELD, so they must not wait for "Save & apply" and must not drag
// apply_network along with them.
//
// Run directly: node tests/ui_power_save.js  (also driven by test_ui_power_save.py)

const { makeSandbox } = require("./ui_sandbox");

let failed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }

// loadConfig() touches cfg.wifi.* unconditionally (auto_channel, client.ipv4,
// client.prefix) on its way to the part these tests care about, so the fixture
// carries a minimal wifi block even though nothing here asserts on it.
const WIFI = { mode: "ap", auto_channel: true, channel: 6, client: { ipv4: "dhcp", prefix: 24 } };
const CFG = {
  locale: "en", wifi: WIFI,
  system: { power_save: { enabled: true, idle_min: 15, off_min: 15 } },
};

function harness() {
  const calls = [];
  const reply = (body) => Promise.resolve({
    ok: true, status: 200, json: () => Promise.resolve(body),
  });
  const sb = makeSandbox({
    fetch: (path, opts) => {
      calls.push({ path, opts });
      if (path === "/api/config") return reply(CFG);
      return reply({ ok: true });
    },
  });
  return { sb, calls };
}

const now = (sb) => sb.document.querySelector("#powerSaveNow").textContent;

test("the countdown is the board's idle clock, not the browser's", async () => {
  const { sb } = harness();
  await sb.loadConfig();          // power_save.enabled: true in CFG
  sb.renderPowerSave({ status: "error", ecu_idle_s: 42 });
  assert(now(sb) === "42 s", "under a minute reads in seconds, got " + now(sb));
  sb.renderPowerSave({ status: "error", ecu_idle_s: 14 * 60 + 30 });
  assert(now(sb) === "14 min", "and rounds down to whole minutes, got " + now(sb));
  // a board that has never seen the ECU still counts -- from its own start
  sb.renderPowerSave({ status: "searching", ecu_idle_s: 3600 });
  assert(now(sb) === "60 min", "got " + now(sb));
});

test("a live link says so instead of showing a stalled zero", async () => {
  const { sb } = harness();
  await sb.loadConfig();
  sb.renderPowerSave({ status: "connected", ecu_idle_s: 0.2 });
  assert(now(sb) === "ECU is answering", "got " + now(sb));
  assert(!/^0/.test(now(sb)), "a zero there reads as a timer that stopped");
});

test("with the feature off, the corner shows nothing tied to nothing", async () => {
  const { sb } = harness();
  const off = { locale: "en", wifi: WIFI,
                system: { power_save: { enabled: false, idle_min: 15, off_min: 15 } } };
  sb.fetch = (path) => Promise.resolve({
    ok: true, status: 200, json: () => Promise.resolve(path === "/api/config" ? off : { ok: true }),
  });
  await sb.loadConfig();
  sb.renderPowerSave({ status: "error", ecu_idle_s: 3600 });
  assert(now(sb) === "", "a countdown for a feature that is off is not tied to anything: " + now(sb));
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok " + name); }
    catch (e) { failed++; console.error("FAIL " + name + "\n  " + e.message); }
  }
  process.exit(failed ? 1 : 0);
})();
