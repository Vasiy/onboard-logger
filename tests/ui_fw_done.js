"use strict";
// Headless harness for the end-of-operation notice: when a firmware read or write
// finishes, a dialog has to say so whichever tab is open, and after a good one it has
// to carry the reminder to cycle the ignition. app.js is evaluated whole in a vm on the
// shared DOM stub and the watcher is driven through its own tick, as the 3 s timer would.
//
// Run directly: node tests/ui_fw_done.js   (also driven by test_ui_fw_done.py)

const { makeSandbox: baseSandbox } = require("./ui_sandbox");

function makeSandbox() {
  const board = { fw: { op: "idle", result: "", last_op: "", current: "", progress: "" } };
  const sb = baseSandbox({
    fetch(url) {
      const body = url === "/api/firmware" ? board.fw : {};
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
    },
  });
  sb.board = board;
  return sb;
}

const settle = () => new Promise((r) => setImmediate(r));
let failed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }

// every node the app made, so a dialog's title/text/reminder can be found by what it says
const said = (sb) => sb.created.map((n) => n.textContent).filter(Boolean);
const dialogs = (sb) => sb.document.body.children.filter((n) => n.className === "modal-back");
const tick = async (sb) => { await sb.fwWatchTick(); await settle(); };

test("a finished read announces itself and reminds to cycle the ignition", async () => {
  const sb = makeSandbox();
  await settle();
  sb.board.fw = { op: "reading", result: "", last_op: "reading", current: "dump.bin" };
  sb.fwWatchStart(true);
  await settle();
  assert(dialogs(sb).length === 0, "nothing to say while it runs");
  sb.board.fw = { op: "idle", result: "ok", last_op: "reading", current: "dump.bin" };
  await tick(sb);
  assert(dialogs(sb).length === 1, "one dialog expected, got " + dialogs(sb).length);
  const texts = said(sb);
  assert(texts.includes("Reading finished"), "title missing: " + JSON.stringify(texts));
  assert(texts.includes("dump.bin"), "the file name should be in the dialog");
  assert(texts.some((x) => /OFF.*5.10 seconds.*ON/.test(x)), "ignition reminder missing");
  await tick(sb);
  assert(dialogs(sb).length === 1, "the same end must not be announced twice");
});

test("a finished write says writing, and the reminder is there too", async () => {
  const sb = makeSandbox();
  await settle();
  sb.board.fw = { op: "writing", result: "", last_op: "writing", current: "a.bin" };
  sb.fwWatchStart(true);
  await settle();
  sb.board.fw = { op: "idle", result: "ok", last_op: "writing", current: "a.bin" };
  await tick(sb);
  const texts = said(sb);
  assert(texts.includes("Writing finished"), JSON.stringify(texts));
  assert(texts.some((x) => /ignition|OFF/i.test(x) && /5.10/.test(x)));
});

test("a failure says so and does NOT tell the rider to power-cycle", async () => {
  const sb = makeSandbox();
  await settle();
  sb.board.fw = { op: "writing", result: "", last_op: "writing", current: "a.bin" };
  sb.fwWatchStart(true);
  await settle();
  sb.board.fw = { op: "idle", result: "error", last_op: "writing", current: "a.bin",
                  progress: "ERROR: Short read" };
  await tick(sb);
  const texts = said(sb);
  assert(texts.includes("Operation failed"), JSON.stringify(texts));
  assert(texts.some((x) => x.includes("ERROR: Short read")), "the reason should be shown");
  assert(!texts.some((x) => /5.10 seconds/.test(x)), "no ignition reminder after a failure");
});

test("a cancelled operation is announced without the reminder", async () => {
  const sb = makeSandbox();
  await settle();
  sb.board.fw = { op: "reading", result: "", last_op: "reading", current: "x.bin" };
  sb.fwWatchStart(true);
  await settle();
  sb.board.fw = { op: "idle", result: "cancelled", last_op: "reading", current: "x.bin" };
  await tick(sb);
  assert(said(sb).includes("cancelled"));
  assert(!said(sb).some((x) => /5.10 seconds/.test(x)));
});

test("an old result found on page load is not announced", async () => {
  const sb = makeSandbox();
  sb.board.fw = { op: "idle", result: "ok", last_op: "reading", current: "old.bin" };
  sb.fwWatchStart(false);
  await settle();
  await tick(sb);
  assert(dialogs(sb).length === 0, "an operation nobody saw start must stay quiet");
});

test("an operation that ended before the first look is still announced when we started it", async () => {
  const sb = makeSandbox();
  await settle();
  sb.board.fw = { op: "idle", result: "ok", last_op: "writing", current: "fast.bin" };
  sb.fwWatchStart(true);
  await settle();
  assert(dialogs(sb).length === 1, "started here, so the end is ours to announce");
});

test("a page opened mid-operation announces its end", async () => {
  const sb = makeSandbox();
  sb.board.fw = { op: "reading", result: "", last_op: "reading", current: "m.bin" };
  await settle();
  await tick(sb);                                  // init() already looked once
  sb.board.fw = { op: "idle", result: "ok", last_op: "reading", current: "m.bin" };
  await tick(sb);
  assert(dialogs(sb).length === 1);
});

test("the reminder exists in every language", () => {
  const sb = makeSandbox();
  for (const loc of Object.keys(sb.window.I18N)) {
    const v = sb.window.I18N[loc]["fw.ignitionCycle"];
    assert(v && /5\D{1,4}10/.test(v), loc + " lacks the ignition reminder");   // "5–10", "5 à 10"
  }
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok", name); }
    catch (e) { failed++; console.log("FAIL", name, "-", e.message); }
  }
  console.log(failed ? `${failed} failed` : `${tests.length} passed`);
  process.exit(failed ? 1 : 0);
})();
