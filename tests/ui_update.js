"use strict";
// Headless harness for the software-update panel in Config -> System.
//
// The board decides everything that matters (app/web/update.py); this panel only
// shows the verdict, so what is worth testing here is that it shows the *right*
// one: the running version, a translated refusal instead of a raw key, a bar fed
// from the nested `prog`, and — the part a rider cannot undo — that nothing is
// uploaded until the confirm dialog has been answered.
//
// Run directly: node tests/ui_update.js   (also driven by test_ui_update.py)

const { makeSandbox: baseSandbox, read } = require("./ui_sandbox");

const IDLE = {
  op: "idle", last_op: "", result: "", progress: "", prog: { percent: -1, done: 0, total: 0 },
  current: "", log: [], log_file: "", available: true, busy: "",
  version: "0.2.0 66f5b2d main 2026-09-06 12:33", free: 1e9, total: 4e9,
};

function makeSandbox(state = IDLE) {
  const posts = [];
  const sb = baseSandbox({
    fetch(url, opts) {
      if (opts && opts.method === "POST") {
        posts.push([url, opts.body]);
        if (sb.uploadError) {
          return Promise.resolve({
            ok: false, status: 409, statusText: "Conflict",
            json: () => Promise.resolve({ error: sb.uploadError }),
          });
        }
      }
      const body = url === "/api/update" ? sb.state
        : url === "/api/config" ? { logging: { zip_after: false }, diag: { zip_after: false } }
        : { files: [] };
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
    },
  });
  sb.posts = posts;
  sb.state = state;
  sb.uploadError = "";
  return sb;
}

let failed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }
function has(hay, needle) {
  assert(String(hay).includes(needle), `expected to contain ${JSON.stringify(needle)}, got ${JSON.stringify(hay)}`);
}
const text = (sb, sel) => sb.document.querySelector(sel).textContent;

// The upload handler awaits confirmDialog, so the harness has to answer it the
// way a thumb does: find the modal and click one of its two buttons.
async function answerDialog(sb, yes) {
  for (let i = 0; i < 5 && !modal(sb); i++) await Promise.resolve();
  const back = modal(sb);
  assert(back, "no confirm dialog was opened");
  const btn = back.querySelector(`[data-act="${yes ? "yes" : "no"}"]`);
  await btn.handlers.click[0]({ currentTarget: btn, target: btn });
}
const modal = (sb) => sb.document.body.children.find(
  (c) => c.classList && c.classList.contains("modal-back") && !c.detached);

const pickFile = (sb) => {
  const input = sb.document.querySelector("#updUpload");
  const file = { name: "onboard-logger-0.2.0-66f5b2d.tar.gz", size: 440000 };
  return input.handlers.change[0]({ currentTarget: input, target: { files: [file], value: "" } });
};

test("the panel names the version the board is actually running", async () => {
  const sb = makeSandbox();
  await sb.window.loadUpdate();
  has(text(sb, "#updVersion"), "0.2.0");
  has(text(sb, "#updVersion"), "66f5b2d");
  has(text(sb, "#updState"), sb.window.I18N.en["upd.idle"]);
});

test("an unstamped board says so instead of showing an empty line", async () => {
  const sb = makeSandbox({ ...IDLE, version: "" });
  await sb.window.loadUpdate();
  has(text(sb, "#updVersion"), sb.window.I18N.en["upd.unknownVersion"]);
});

test("a refusal is shown translated, never as a bare key", async () => {
  const sb = makeSandbox({ ...IDLE, busy: "err.update_fw_busy" });
  await sb.window.loadUpdate();
  const state = text(sb, "#updState");
  has(state, sb.window.I18N.en["err.update_fw_busy"]);
  assert(!state.includes("err.update_"), "the raw i18n key reached the UI: " + state);
});

test("the bar is fed from the nested prog, which the disk free/total cannot eat", async () => {
  const sb = makeSandbox({ ...IDLE, op: "testing", last_op: "testing",
                           prog: { percent: 40, done: 8, total: 20 } });
  await sb.window.loadUpdate();
  assert(sb.document.querySelector("#updProg").hidden === false, "the bar should be visible");
  assert(sb.document.querySelector("#updFill").style.width === "40%",
         "got " + sb.document.querySelector("#updFill").style.width);
  has(text(sb, "#updVal"), "8 / 20");        // not 1000000000 / 4000000000
  has(text(sb, "#updState"), sb.window.I18N.en["upd.testing"]);
});

test("a finished operation keeps its bar and says how it ended", async () => {
  const sb = makeSandbox({ ...IDLE, op: "idle", last_op: "swapping", result: "ok",
                           prog: { percent: 100, done: 20, total: 20 } });
  await sb.window.loadUpdate();
  assert(sb.document.querySelector("#updProg").hidden === false,
         "the bar outlives the operation, like the firmware one");
  has(text(sb, "#updState"), sb.window.I18N.en["upd.ok"]);
});

test("nothing is uploaded until the confirm dialog is answered yes", async () => {
  const sb = makeSandbox();
  const p = pickFile(sb);
  assert(sb.posts.length === 0, "the archive went out before the rider agreed");
  await answerDialog(sb, false);
  await p;
  assert(!sb.posts.some(([u]) => u === "/api/update/upload"), "cancel still uploaded");

  const p2 = pickFile(sb);
  await answerDialog(sb, true);
  await p2;
  const up = sb.posts.filter(([u]) => u === "/api/update/upload");
  assert(up.length === 1, "expected exactly one upload, got " + up.length);
  assert(up[0][1] && up[0][1].isFormData, "the archive must go as multipart form data");
});

test("a rejected upload is toasted in the rider's language", async () => {
  const sb = makeSandbox();
  sb.uploadError = "err.update_logging";
  const p = pickFile(sb);
  await answerDialog(sb, true);
  await p;
  const toasts = (sb.document.querySelector("#toasts").children || []).map((c) => c.textContent);
  assert(toasts.length, "a refused upload must say why");
  has(toasts.join(" | "), sb.window.I18N.en["err.update_logging"]);
  assert(!toasts.join(" | ").includes("err.update_"), "the raw key reached the toast");
});

test("the panel is markup, not injected by script", () => {
  const html = read("app/static/index.html");
  for (const id of ["updUpload", "updVersion", "updState", "updProg", "updTrack",
                    "updFill", "updVal", "updLog", "updLogBtn"]) {
    has(html, `id="${id}"`);
  }
  has(html, 'accept=".tar.gz,.tgz,.tar,.zip"');
});

test("every locale carries the update panel's strings", () => {
  const sb = makeSandbox();
  const keys = ["upd.title", "upd.hint", "upd.upload", "upd.showLog", "upd.progress",
                "upd.confirm", "upd.apply", "upd.started", "upd.unknownVersion",
                "upd.idle", "upd.unpacking", "upd.testing", "upd.swapping",
                "upd.restarting", "upd.ok", "upd.error",
                "err.update_busy", "err.update_fw_busy", "err.update_logging",
                "err.update_scanning", "err.update_bad_archive",
                "err.update_unsafe_member", "err.update_too_big",
                "err.update_no_space", "err.update_incomplete",
                "err.update_deps_changed", "err.update_import_failed",
                "err.update_tests_failed", "err.update_swap_failed",
                "err.update_failed"];
  for (const [loc, table] of Object.entries(sb.window.I18N)) {
    for (const key of keys) {
      assert(typeof table[key] === "string" && table[key], `${loc} is missing ${key}`);
    }
  }
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok " + name); }
    catch (e) { failed++; console.log("FAIL " + name + ": " + (e && e.message)); }
  }
  process.exit(failed ? 1 : 0);
})();
