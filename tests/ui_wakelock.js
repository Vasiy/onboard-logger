"use strict";
// Headless harness for the keep-awake switch.
//
// It is the third device-local preference beside paramMode and theme, so the
// thing that matters most is that it never travels to /api/config. The second
// is the fallback: navigator.wakeLock needs a secure context and the board
// serves plain HTTP over its own AP, so on the bike the API is simply absent
// and the muted looping clip is all there is. The sandbox ships `navigator: {}`
// and `isSecureContext: false` for exactly that case.
//
// Run directly: node tests/ui_wakelock.js  (also driven by test_ui_wakelock.py)

const { makeSandbox } = require("./ui_sandbox");

let failed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }

const videos = (sb) => sb.created.filter((n) => n.tagName === "video");

test("with no wakeLock API the switch falls back to a muted looping clip", async () => {
  const sb = makeSandbox();
  assert(!("wakeLock" in sb.navigator), "the sandbox stands in for the board's http:// case");
  sb.setWake(true);
  const v = videos(sb);
  assert(v.length === 1, "one clip, got " + v.length);
  assert(v[0].muted === true && v[0].loop === true, "a clip with sound or one that ends is useless");
  // iOS takes a clip fullscreen without this, which would cover the whole UI
  assert(v[0].attrs["playsinline"] !== undefined, "playsinline is what keeps it out of the way");
  assert(String(v[0].src).startsWith("data:video/mp4;base64,"),
         "the clip is inlined: the SPA has no build step and no dependencies");
  assert(v[0].parentNode === sb.document.body, "an element outside the document is not playing");
});

test("the preference is device-local and never reaches the board", async () => {
  const posts = [];
  const sb = makeSandbox({
    fetch: (url, opt) => {
      posts.push([url, opt && opt.method]);
      return Promise.reject(new Error("offline"));
    },
  });
  posts.length = 0;                       // init() reads /api/config once at boot
  sb.setWake(true);
  assert(sb.localStorage.getItem("wakeLock") === "1", "on -> localStorage");
  sb.setWake(false);
  assert(sb.localStorage.getItem("wakeLock") === "0", "off -> localStorage");
  assert(posts.length === 0,
         "a phone preference must not travel to the board: " + JSON.stringify(posts));
});

test("a stored preference arms the checkbox at load", async () => {
  const on = makeSandbox({ storage: { wakeLock: "1" } });
  assert(on.document.querySelector("#wakeToggle").checked === true,
         "init() should reflect the stored choice");
  const off = makeSandbox();
  assert(off.document.querySelector("#wakeToggle").checked === false,
         "and default to off when nothing is stored");
});

test("where the real API exists it is used and no clip is made", async () => {
  const sb = makeSandbox();
  let asked = null, released = 0;
  sb.navigator.wakeLock = {
    request: (kind) => {
      asked = kind;
      return Promise.resolve({ addEventListener() {}, release() { released++; } });
    },
  };
  sb.setWake(true);
  await new Promise((r) => setTimeout(r, 0));   // the request resolves off the tick
  assert(asked === "screen", "the screen lock is the one worth asking for, got " + asked);
  assert(videos(sb).length === 0, "no clip when the platform will do it properly");
  sb.setWake(false);
  assert(released === 1, "switching off has to hand the lock back, got " + released);
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok " + name); }
    catch (e) { failed++; console.error("FAIL " + name + "\n  " + e.message); }
  }
  process.exit(failed ? 1 : 0);
})();
