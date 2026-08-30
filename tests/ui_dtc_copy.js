"use strict";
// Headless harness for the Fault-codes "Copy" button.
//
// app/static/app.js is a plain script with no exports, so it is evaluated whole
// inside a vm context on a minimal DOM stub: its top-level `function`
// declarations land on that context's global and can be called from here.
// (`let`/`const` at the top level do not — hence the assertions go through
// setDtcCopy()/renderDtc() and the button's own click handler, never through
// the module-private dtcCopyText.)
//
// Run directly: node tests/ui_dtc_copy.js   (also driven by test_ui_dtc_copy.py)

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const read = (p) => fs.readFileSync(path.join(ROOT, p), "utf8");

process.on("unhandledRejection", () => {});   // init()'s fetches have nowhere to go here

// ---------- DOM stub ----------
function makeSandbox() {
  const created = [];

  function el(tag = "div") {
    const node = {
      tagName: tag,
      children: [],
      parentNode: null,
      handlers: {},
      dataset: {},
      style: {},
      attrs: {},
      value: "",
      textContent: "",
      innerHTML: "",
      hidden: false,
      disabled: false,
      classList: {
        _s: new Set(),
        add(...c) { c.forEach((x) => this._s.add(x)); },
        remove(...c) { c.forEach((x) => this._s.delete(x)); },
        toggle(c, on) {
          const want = on === undefined ? !this._s.has(c) : !!on;
          if (want) this._s.add(c); else this._s.delete(c);
        },
        contains(c) { return this._s.has(c); },
      },
      addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
      removeEventListener() {},
      setAttribute(k, v) { this.attrs[k] = v; },
      getAttribute(k) { return this.attrs[k]; },
      appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
      removeChild(c) { this.children = this.children.filter((x) => x !== c); },
      remove() {
        if (node.parentNode) node.parentNode.removeChild(node);
        node.parentNode = null;
        node.detached = true;
      },
      querySelector() { return el(); },
      querySelectorAll() { return []; },
      setSelectionRange(a, b) { node.selectedRange = [a, b]; },
      focus() {}, blur() {}, click() {}, scrollIntoView() {}, requestSubmit() {},
    };
    created.push(node);
    return node;
  }

  const bySel = new Map();
  const document = {
    documentElement: el("html"),
    body: el("body"),
    activeElement: null,
    execCalls: [],
    execResult: true,
    querySelector(sel) {
      if (!bySel.has(sel)) bySel.set(sel, el());
      return bySel.get(sel);
    },
    querySelectorAll() { return []; },
    createElement(tag) { return el(tag); },
    createRange() { return { selectNodeContents() {} }; },
    createTextNode(txt) { const n = el("#text"); n.textContent = txt; return n; },
    execCommand(cmd) { document.execCalls.push(cmd); return document.execResult; },
    addEventListener() {}, removeEventListener() {},
  };

  const sandbox = {
    console,
    document,
    created,
    isSecureContext: false,
    navigator: {},                       // no clipboard by default: the board's http:// case
    location: { protocol: "http:", host: "192.168.4.1" },
    localStorage: {
      s: {},
      getItem(k) { return k in this.s ? this.s[k] : null; },
      setItem(k, v) { this.s[k] = String(v); },
      removeItem(k) { delete this.s[k]; },
    },
    // no real timers: app.js arms a 1 s ticker and toasts auto-dismiss, either of
    // which would keep node alive long after the assertions are done
    setTimeout: () => 0, clearTimeout: () => {},
    setInterval: () => 0, clearInterval: () => {},
    requestAnimationFrame: () => 0,
    fetch: () => Promise.reject(new Error("offline")),
    WebSocket: function WebSocket() { return { close() {}, send() {} }; },
    getSelection: () => ({ removeAllRanges() {}, addRange() {} }),
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(read("app/static/i18n.js"), sandbox, { filename: "i18n.js" });
  vm.runInContext(read("app/static/app.js"), sandbox, { filename: "app.js" });
  return sandbox;
}

// ---------- tiny test runner ----------
let failed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }
function has(hay, needle) {
  assert(String(hay).includes(needle), `expected to contain ${JSON.stringify(needle)}, got ${JSON.stringify(hay)}`);
}

const RES = {
  ok: true,
  count: 2,
  raw: "58 02 01 07 2a 01 30 24",
  dtcs: [
    { code: "P0107", desc: "MAP sensor low", status: 0x2a, bits: "stored", stored: true, kind: 2, warn: true },
    { code: "P0130", desc: "Lambda sensor", status: 0x0a, bits: "current", stored: false, kind: 2, warn: false },
  ],
};

// Fires the Copy button exactly as a tap does and returns what reached the clipboard.
async function clickCopy(sb) {
  const btn = sb.document.querySelector("#dtcCopyBtn");
  const click = btn.handlers.click && btn.handlers.click[0];
  assert(click, "#dtcCopyBtn has no click handler");
  await click({ currentTarget: btn });
  return btn;
}
const lastToast = (sb) => {
  const box = sb.document.querySelector("#toasts");
  return box.children[box.children.length - 1];
};

test("copy button stays hidden until there is a result", () => {
  const sb = makeSandbox();
  const btn = sb.document.querySelector("#dtcCopyBtn");
  sb.setDtcCopy("");
  assert(btn.hidden === true, "button should be hidden with an empty buffer");
  sb.renderDtc("read_dtc", RES);
  assert(btn.hidden === false, "button should appear once codes are rendered");
  sb.setDtcCopy("");
  assert(btn.hidden === true, "button should hide again when the box is cleared");
});

test("copied text carries every code, its status byte and the raw frame", async () => {
  const sb = makeSandbox();
  const writes = [];
  sb.isSecureContext = true;
  sb.navigator.clipboard = { writeText: (s) => { writes.push(s); return Promise.resolve(); } };
  sb.renderDtc("read_dtc", RES);
  await clickCopy(sb);
  assert(writes.length === 1, "clipboard.writeText should be called once");
  const txt = writes[0];
  has(txt, "Fault codes (DTC)");
  has(txt, "2 fault code(s)");
  has(txt, "P0107");
  has(txt, "MAP sensor low");
  has(txt, "stored");
  has(txt, "0x2a");
  has(txt, "P0130");
  has(txt, "current");
  has(txt, "0x0a");
  has(txt, "⚠");
  has(txt, RES.raw);
  assert(!txt.includes("<"), "clipboard text must be plain, not the rendered HTML");
  assert(txt.split("\n").length === 5, "head + count + 2 codes + raw, got:\n" + txt);
  assert(lastToast(sb).textContent === sb.window.I18N.en["test.copied"], "success toast expected");
});

test("http:// board falls back to execCommand instead of the missing clipboard API", async () => {
  const sb = makeSandbox();                 // isSecureContext:false, navigator.clipboard undefined
  sb.renderDtc("read_dtc", RES);
  await clickCopy(sb);
  assert(sb.document.execCalls.join() === "copy", "expected a single execCommand('copy')");
  const ta = sb.created.filter((n) => n.tagName === "textarea").pop();
  assert(ta, "fallback should stage the text in a textarea");
  has(ta.value, "P0107");
  assert(ta.attrs.readonly !== undefined, "textarea must be readonly");
  assert(ta.contentEditable === "true", "iOS needs contentEditable to allow the selection");
  assert(ta.selectedRange && ta.selectedRange[1] === ta.value.length, "whole text must be selected");
  assert(ta.detached === true, "the staging textarea must be removed again");
  assert(lastToast(sb).textContent === sb.window.I18N.en["test.copied"], "success toast expected");
});

test("secure context that refuses the clipboard still copies via the fallback", async () => {
  const sb = makeSandbox();
  sb.isSecureContext = true;
  sb.navigator.clipboard = { writeText: () => Promise.reject(new Error("denied")) };
  sb.renderDtc("read_dtc", RES);
  await clickCopy(sb);
  assert(sb.document.execCalls.join() === "copy", "a rejected writeText must fall through");
  assert(lastToast(sb).textContent === sb.window.I18N.en["test.copied"], "success toast expected");
});

test("a refused copy tells the user instead of pretending", async () => {
  const sb = makeSandbox();
  sb.document.execResult = false;
  sb.renderDtc("read_dtc", RES);
  await clickCopy(sb);
  assert(lastToast(sb).textContent === sb.window.I18N.en["test.copyFail"], "failure toast expected");
});

test("cleared / none / error results are copyable too", () => {
  const sb = makeSandbox();
  const btn = sb.document.querySelector("#dtcCopyBtn");
  const writes = [];
  sb.isSecureContext = true;
  sb.navigator.clipboard = { writeText: (s) => { writes.push(s); return Promise.resolve(); } };

  sb.renderDtc("clear_dtc", { ok: true });
  assert(btn.hidden === false);
  sb.renderDtc("read_dtc", { ok: true, count: 0, dtcs: [] });
  assert(btn.hidden === false);
  sb.renderDtc("read_dtc", { ok: false, error: "err.no_response", detail: "timeout" });
  assert(btn.hidden === false, "an error is worth copying — that is what gets pasted into a forum post");
});

test("the copied error text is the translated message, not the i18n key", async () => {
  const sb = makeSandbox();
  const writes = [];
  sb.isSecureContext = true;
  sb.navigator.clipboard = { writeText: (s) => { writes.push(s); return Promise.resolve(); } };
  sb.renderDtc("read_dtc", { ok: false, error: "err.no_response", detail: "timeout" });
  await clickCopy(sb);
  has(writes[0], sb.window.I18N.en["err.no_response"]);
  has(writes[0], "(timeout)");
  assert(!writes[0].includes("err.no_response"), "raw key must not reach the clipboard");
});

test("copying nothing is a no-op, not an empty clipboard write", async () => {
  const sb = makeSandbox();
  const writes = [];
  sb.isSecureContext = true;
  sb.navigator.clipboard = { writeText: (s) => { writes.push(s); return Promise.resolve(); } };
  sb.setDtcCopy("");
  await clickCopy(sb);
  assert(writes.length === 0, "must not overwrite the clipboard with an empty string");
  assert(sb.document.execCalls.length === 0, "must not reach the fallback either");
  assert(lastToast(sb).textContent === sb.window.I18N.en["test.copyFail"]);
});

test("every locale defines the three copy strings", () => {
  const sb = makeSandbox();
  for (const [loc, table] of Object.entries(sb.window.I18N)) {
    for (const key of ["test.copy", "test.copied", "test.copyFail"]) {
      assert(typeof table[key] === "string" && table[key], `${loc} is missing ${key}`);
    }
  }
});

test("the button is markup, not injected by script", () => {
  const html = read("app/static/index.html");
  has(html, 'id="dtcCopyBtn"');
  has(html, 'data-i18n="test.copy"');
  // it must start hidden: an empty result box with a live Copy button reads as broken
  const tag = html.match(/<button id="dtcCopyBtn"[^>]*>/)[0];
  has(tag, "hidden");
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok " + name); }
    catch (e) { failed++; console.log("FAIL " + name + ": " + (e && e.message)); }
  }
  process.exit(failed ? 1 : 0);
})();
