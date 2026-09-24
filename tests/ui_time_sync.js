"use strict";
// Headless harness for the one clock the bike has: the phone's.
//
// Without a battery-backed module wired on (app/web/rtc.py finds one by driver
// name when there is), the board comes up at whatever time it was
// last shut down with and file names, CSV timestamps and the day folder every
// log lands in all come from that clock. The browser offers its own at boot —
// but a phone that keeps the page open reconnects only the websocket when the
// board (or its AP) comes back, and never loads the page again. That is how a
// whole ride was written into the previous day's folder on 2026-09-10, with the
// rider watching live curves the entire time.
//
// Run directly: node tests/ui_time_sync.js  (also driven by test_ui_time_sync.py)

const { makeSandbox } = require("./ui_sandbox");

let failed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }

const flush = async () => { for (let i = 0; i < 20; i++) await new Promise(setImmediate); };

// a sandbox whose /api/config answers with the given system block; every other
// call resolves empty so init() runs to the end
function sandbox(system, posts) {
  return makeSandbox({
    fetch: (url, opt) => {
      if (opt && opt.method === "POST") posts.push([url, JSON.parse(opt.body)]);
      const body = url === "/api/config" ? { locale: "en", system } : {};
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body),
                               text: () => Promise.resolve("") });
    },
  });
}

const syncs = (posts) => posts.filter((p) => p[0] === "/api/system/time/auto");

test("the clock is offered on every socket, not only on page load", async () => {
  const posts = [];
  const sb = sandbox({ auto_time_sync: true }, posts);
  await flush();
  assert(syncs(posts).length === 1, "boot offers the clock once");
  const epoch = syncs(posts)[0][1].epoch;
  assert(epoch > 1600000000, "an epoch in seconds, not milliseconds: " + epoch);

  assert(sb.sockets.length === 1, "init opened the live socket");
  sb.sockets[0].onopen();               // the board came back; no page load
  await flush();
  assert(syncs(posts).length === 2, "a reconnect must offer the clock again");
});

test("the switch in Config still turns the offer off", async () => {
  const posts = [];
  const sb = sandbox({ auto_time_sync: false }, posts);
  await flush();
  sb.sockets[0].onopen();
  await flush();
  assert(syncs(posts).length === 0, "auto_time_sync=false means never: " + JSON.stringify(posts));
});

(async () => {
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log("ok  " + name);
    } catch (e) {
      failed++;
      console.log("FAIL " + name + "\n     " + e.message);
    }
  }
  console.log(`\n${tests.length - failed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
