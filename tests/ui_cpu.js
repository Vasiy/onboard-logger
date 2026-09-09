"use strict";
// Headless harness for the Processor block.
//
// It exists because of a bug it would have caught on the first run: api() hands
// its second argument straight to fetch as RequestInit, so passing the payload
// alone sent a GET. That reached the status endpoint, which has no `ok` in it,
// and the panel reported "could not change the processor settings" for a change
// it had never asked for. So the assertions here are about the request as much
// as the answer.
//
// Run directly: node tests/ui_cpu.js  (also driven by test_ui_cpu.py)

const { makeSandbox } = require("./ui_sandbox");

let failed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }

// what the board answered on 2026-09-09
const STATUS = {
  available: true,
  governors: ["conservative", "ondemand", "userspace", "powersave", "performance", "schedutil"],
  freqs_khz: [408000, 600000, 816000, 1008000, 1200000, 1296000],
  governor: "ondemand", max_khz: 1296000, hw_max_khz: 1296000, cur_khz: 816000,
  temp_c: 64.6, trip_c: 70.0, headroom_c: 5.4,
};

function harness(over = {}) {
  const calls = [];
  const reply = (body) => Promise.resolve({
    ok: true, status: 200, json: () => Promise.resolve(body),
  });
  const sb = makeSandbox({
    fetch: (path, opts) => {
      calls.push({ path, opts });
      if (path === "/api/system/cpu") {
        const post = opts && opts.method === "POST";
        return reply(post ? { ...STATUS, ok: true, applied: ["governor powersave"], ...over }
                          : STATUS);
      }
      if (path === "/api/config") return reply({ locale: "en", system: { cpu: {} } });
      return Promise.reject(new Error("offline"));
    },
  });
  return { sb, calls };
}

const fire = (el, type) => (el.handlers[type] || []).forEach((fn) => fn({ target: el }));
const cpuCalls = (calls) => calls.filter((c) => c.path === "/api/system/cpu");

test("the lists come from the board, with an empty 'leave alone' first", async () => {
  const { sb } = harness();
  await sb.loadCpu();
  const gov = sb.document.querySelector("#cpuGov");
  const max = sb.document.querySelector("#cpuMax");
  assert(/value=""/.test(gov.innerHTML), "an empty option is how you say 'do not touch'");
  for (const g of STATUS.governors) assert(gov.innerHTML.includes(g), "missing governor " + g);
  assert(max.innerHTML.includes("408000") && max.innerHTML.includes("1296 MHz"),
         "frequencies come as kHz values shown in MHz: " + max.innerHTML);
  assert(sb.document.querySelector("#cpuBox").hidden === false, "the block shows on a real board");
});

test("a change the board accepts reports success, not the failure text", async () => {
  const { sb } = harness();
  await sb.loadCpu();
  const gov = sb.document.querySelector("#cpuGov");
  gov.value = "powersave";
  fire(gov, "change");
  await new Promise((r) => setTimeout(r, 0));
  const said = sb.created
    .filter((n) => String(n.className || "").startsWith("toast "))
    .map((n) => [n.className, n.textContent]);
  assert(said.length === 1, "one toast, got " + JSON.stringify(said));
  assert(said[0][0].includes("toast--ok"), "an accepted change is not a warning: " + said[0][0]);
  assert(said[0][1] === sb.window.I18N.en["cfg.saved"], "it says it saved: " + said[0][1]);
  // and the live line is repainted from the answer rather than left stale
  assert(sb.document.querySelector("#cpuNow").textContent.includes("64.6"),
         "the reply repaints the live line");
});

test("changing the governor sends a POST with a JSON body", async () => {
  const { sb, calls } = harness();
  await sb.loadCpu();
  const gov = sb.document.querySelector("#cpuGov");
  gov.value = "powersave";
  fire(gov, "change");
  await new Promise((r) => setTimeout(r, 0));
  const post = cpuCalls(calls).find((c) => c.opts && c.opts.method === "POST");
  assert(post, "the change has to go out as a POST, not a bare GET: "
               + JSON.stringify(cpuCalls(calls).map((c) => c.opts && c.opts.method)));
  assert(/application\/json/.test(post.opts.headers["Content-Type"]), "declare the body type");
  const body = JSON.parse(post.opts.body);
  assert(body.governor === "powersave", "governor in the body, got " + JSON.stringify(body));
  assert(body.max_khz === 0, "an untouched ceiling is 0, meaning leave it alone");
});

test("changing the ceiling alone sends the number and no governor", async () => {
  const { sb, calls } = harness();
  await sb.loadCpu();
  const max = sb.document.querySelector("#cpuMax");
  max.value = "816000";
  fire(max, "change");
  await new Promise((r) => setTimeout(r, 0));
  const body = JSON.parse(cpuCalls(calls).find((c) => c.opts && c.opts.method === "POST").opts.body);
  assert(body.max_khz === 816000, "kHz as a number, got " + JSON.stringify(body));
  assert(body.governor === "", "nothing was picked, so nothing is asked for");
});

test("a board that refuses says why instead of claiming success", async () => {
  const { sb } = harness({ ok: false, message: "governor turbo is not offered" });
  await sb.loadCpu();
  const gov = sb.document.querySelector("#cpuGov");
  gov.value = "powersave";
  fire(gov, "change");
  await new Promise((r) => setTimeout(r, 0));
  const said = sb.created
    .filter((n) => String(n.className || "").startsWith("toast "))
    .map((n) => n.textContent);
  assert(said.length === 1, "one toast, got " + JSON.stringify(said));
  assert(said[0] === "governor turbo is not offered",
         "the board's own reason is what the rider needs, not a generic failure: " + said[0]);
});

test("a dev host with no cpufreq hides the block rather than offering nothing", async () => {
  const sb = makeSandbox({
    fetch: (path) => {
      if (path === "/api/system/cpu") {
        return Promise.resolve({ ok: true, status: 200,
          json: () => Promise.resolve({ available: false, governors: [], freqs_khz: [],
                                        message: "no cpufreq (dev host?)" }) });
      }
      return Promise.reject(new Error("offline"));
    },
  });
  await sb.loadCpu();
  assert(sb.document.querySelector("#cpuBox").hidden === true, "nothing to offer, nothing to show");
});

test("a ceiling nobody asked for is named in the live line", async () => {
  // the kernel's thermal governor writes scaling_max_freq too, so the board can
  // sit capped with config asking for nothing -- silence there reads as a slow
  // board rather than a throttled one
  const { sb } = harness();
  await sb.loadCpu();
  sb.renderCpuNow({ ...STATUS, max_khz: 600000 });
  const now = sb.document.querySelector("#cpuNow");
  assert(now.textContent.includes("600"), "the live ceiling has to show: " + now.textContent);
  // and at the factory maximum there is nothing to say
  sb.renderCpuNow(STATUS);
  assert(!now.textContent.includes("1296"),
         "an uncapped board should not claim a cap: " + now.textContent);
});

test("the header line carries the temperature and the clock together", async () => {
  const { sb } = harness();
  sb.renderCpuTemp({ cpu_temp: 81.2, cpu_trip: 70, cpu_mhz: 408, cpu_gov: "ondemand" });
  const el = sb.document.querySelector("#cpuTemp");
  assert(el.hidden === false, "a board with a thermal zone shows it");
  assert(el.textContent.includes("81.2") && el.textContent.includes("408"),
         "both numbers, because one without the other does not say 'throttling': " + el.textContent);
  assert(el.classList.contains("is-hot"), "past the trip it has to look wrong");
  sb.renderCpuTemp({ cpu_temp: 0 });
  assert(el.hidden === true, "no thermal zone, no line");
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok " + name); }
    catch (e) { failed++; console.error("FAIL " + name + "\n  " + e.message); }
  }
  process.exit(failed ? 1 : 0);
})();
