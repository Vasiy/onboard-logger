"use strict";
// Headless harness for the fan telemetry readout in Config -> System.
//
// Pinned here: the corner shows nothing while fan.enabled is off, exactly like
// renderPowerSave's "not tied to anything" rule -- a number with the feature
// off would look like a live reading of a service that may not even be
// installed. An RPM reading tagged rpm_valid:false (the 3-wire-fan-on-the-2-
// pin-connector case, where the tach is only meaningful at 100% duty) still
// shows the number, unannotated -- the caveat for why it may be wrong lives
// in README.md's Fan control section now, not repeated on every reading.
//
// Run directly: node tests/ui_fan.js  (also driven by test_ui_fan.py)

const { makeSandbox } = require("./ui_sandbox");

let failed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }

const WIFI = { mode: "ap", auto_channel: true, channel: 6, client: { ipv4: "dhcp", prefix: 24 } };

function harness(fanCfg) {
  const CFG = { locale: "en", wifi: WIFI, fan: fanCfg };
  const reply = (body) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  const sb = makeSandbox({
    fetch: (path) => reply(path === "/api/config" ? CFG : { ok: true }),
  });
  return sb;
}

const now = (sb) => sb.document.querySelector("#fanNow").textContent;

test("with the feature off, the corner shows nothing tied to nothing", async () => {
  const sb = harness({ enabled: false });
  await sb.loadConfig();
  sb.renderFan({ fan_type: 3, fan_temp_c: 46.3, fan_duty_pct: 100, fan_rpm: 1200, fan_rpm_valid: true });
  assert(now(sb) === "", "a reading for a feature that is off is not tied to anything: " + now(sb));
});

test("no data yet from the addon also shows nothing", async () => {
  const sb = harness({ enabled: true });
  await sb.loadConfig();
  sb.renderFan({ fan_type: 0, fan_temp_c: 0, fan_duty_pct: 0, fan_rpm: null, fan_rpm_valid: null });
  assert(now(sb) === "", "got " + now(sb));
});

test("2-pin mode shows temperature and duty, no rpm", async () => {
  const sb = harness({ enabled: true });
  await sb.loadConfig();
  sb.renderFan({ fan_type: 2, fan_temp_c: 46.3, fan_duty_pct: 60, fan_rpm: null, fan_rpm_valid: null });
  const text = now(sb);
  assert(text.includes("46.3"), "got " + text);
  assert(text.includes("60"), "got " + text);
  assert(!/rpm/i.test(text), "fan_type 2 has no tach line, must not claim an rpm: " + text);
});

test("a valid rpm reading is shown plainly", async () => {
  const sb = harness({ enabled: true });
  await sb.loadConfig();
  sb.renderFan({ fan_type: 4, fan_temp_c: 40, fan_duty_pct: 50, fan_rpm: 1200, fan_rpm_valid: true });
  assert(now(sb).includes("1200"), "got " + now(sb));
});

test("an rpm reading flagged invalid still shows the number, just unannotated", async () => {
  const sb = harness({ enabled: true });
  await sb.loadConfig();
  sb.renderFan({ fan_type: 3, fan_temp_c: 40, fan_duty_pct: 50, fan_rpm: 900, fan_rpm_valid: false });
  const text = now(sb);
  assert(text.includes("900"), "the number still shows, so it can be sanity-checked: " + text);
});

// -- PWM frequency slider readout -----------------------------------------
// Matches Tile history's own slider (ui.spark_s, Config -> Display): just
// the number and its unit beside the thumb, not a restated label -- the
// row's own <span> already says "PWM frequency".

test("PWM frequency slider shows just the number and unit, like Tile history's", async () => {
  const sb = harness({ enabled: true, period_ns: 8000000 });  // 1e9 / 8e6 = 125 Hz
  await sb.loadConfig();
  const out = sb.document.querySelector("#fanFreqValue");
  assert(out.textContent === "125 Hz", "got " + JSON.stringify(out.textContent));
});

test("PWM frequency readout re-translates its unit on a locale switch, without drifting off the saved value", async () => {
  const sb = harness({ enabled: true, period_ns: 8000000 });  // 1e9 / 8e6 = 125 Hz
  await sb.loadConfig();
  sb.applyLocale("ru");
  const out = sb.document.querySelector("#fanFreqValue");
  // must stay exactly 125, not whatever the log slider's 101 discrete
  // positions round-trip it to (122) -- period_ns did not change, so the
  // number shown must not either
  assert(out.textContent === "125 Гц", "got " + JSON.stringify(out.textContent));
});

test("changing the PWM frequency rescales duty_cycles_ns proportionally, not just period_ns", async () => {
  // Found live: a frequency change used to save only period_ns, leaving the
  // temperature-tier duty values sized for the old one. A lower frequency
  // then leaves every tier above the new, smaller period -- a real board's
  // kernel rejects that duty_cycle sysfs write outright, and the caller
  // that swallows it (app/main.py's _fan_control) silently stalled fan
  // control forever, with the board still sitting at temperature.
  let posted = null;
  const CFG = { locale: "en", wifi: WIFI,
    fan: { enabled: true, period_ns: 8000000, duty_cycles_ns: [8000000, 6000000, 4000000, 2000000] } };
  const sb = makeSandbox({
    fetch: (path, opts) => {
      if (path === "/api/config" && opts && opts.method === "POST") {
        posted = JSON.parse(opts.body);
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(CFG) });
    },
  });
  await sb.loadConfig();
  const slider = sb.document.querySelector("#fanFreqSlider");
  slider.value = "23";  // a lower frequency than the loaded 125 Hz
  await slider.handlers.change[0]();
  assert(posted && posted.fan, "no /api/config POST captured");
  const newPeriod = posted.fan.period_ns;
  const expected = [8000000, 6000000, 4000000, 2000000].map((d) => Math.round(d / 8000000 * newPeriod));
  assert(JSON.stringify(posted.fan.duty_cycles_ns) === JSON.stringify(expected),
    "got " + JSON.stringify(posted.fan.duty_cycles_ns) + " want " + JSON.stringify(expected));
});

// -- Probe (calibration sweep) result rendering --------------------------
// probeFanGpio() (Config -> System, the Probe button next to Tach GPIO)
// used to show a raw pulse count from a single full-speed sample; it now
// shows what the multi-step sweep found: the duty% the fan starts
// spinning at (min_duty_pct), and its rpm at 100% duty when the sweep's
// last step saw pulses (max_rpm, which can legitimately be absent even on
// an active sweep -- see FanController.probe in app/web/fan.py).

function probeHarness({ status = 200, body }) {
  const CFG = { locale: "en", wifi: WIFI, fan: { enabled: true } };
  return makeSandbox({
    fetch: (path) => {
      if (path === "/api/fan/probe") {
        return Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(CFG) });
    },
  });
}

test("a successful sweep reports where the fan starts spinning and its top rpm", async () => {
  const sb = probeHarness({ body: { gpio: 16, active: true, min_duty_pct: 30, max_rpm: 4200 } });
  await sb.loadConfig();
  sb.document.querySelector("#fanTachGpio").value = "16";
  await sb.probeFanGpio();
  const out = sb.document.querySelector("#fanProbeResult");
  assert(out.textContent.includes("30"), "must show the min duty it found: " + out.textContent);
  assert(out.textContent.includes("4200"), "must show the max rpm it found: " + out.textContent);
  assert(out.className.includes("fan-probe-ok"), out.className);
});

test("a sweep that never sees a pulse reports inactive, no leftover pulse count", async () => {
  const sb = probeHarness({ body: { gpio: 16, active: false, min_duty_pct: null, max_rpm: null } });
  await sb.loadConfig();
  sb.document.querySelector("#fanTachGpio").value = "16";
  await sb.probeFanGpio();
  const out = sb.document.querySelector("#fanProbeResult");
  assert(!/\d/.test(out.textContent), "no stale pulse count to show: " + out.textContent);
  assert(out.className.includes("fan-probe-bad"), out.className);
});

test("an active sweep whose 100% step saw no pulses shows the min duty with no rpm figure", async () => {
  const sb = probeHarness({ body: { gpio: 16, active: true, min_duty_pct: 40, max_rpm: null } });
  await sb.loadConfig();
  sb.document.querySelector("#fanTachGpio").value = "16";
  await sb.probeFanGpio();
  const out = sb.document.querySelector("#fanProbeResult");
  assert(out.textContent.includes("40"), out.textContent);
  assert(out.className.includes("fan-probe-ok"), out.className);
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok " + name); }
    catch (e) { failed++; console.error("FAIL " + name + "\n  " + e.message); }
  }
  process.exit(failed ? 1 : 0);
})();
