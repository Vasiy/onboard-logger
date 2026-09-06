"use strict";
// Headless harness for the Firmware tab's identification UI. Same technique as
// tests/ui_dtc_copy.js: app.js is evaluated whole in a vm on a DOM stub, and the
// assertions go through its own functions (fwFiles and friends are top-level
// `let`, so they never appear on the vm global — everything is driven through
// loadFirmware() with a stubbed api()).
//
// Run directly: node tests/ui_fw_ident.js   (also driven by test_ui_fw_ident.py)

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const read = (p) => fs.readFileSync(path.join(ROOT, p), "utf8");

process.on("unhandledRejection", () => {});

function makeSandbox() {
  const created = [];

  function el(tag = "div") {
    const node = {
      tagName: tag, children: [], parentNode: null, handlers: {}, dataset: {},
      style: {}, attrs: {}, value: "", textContent: "", innerHTML: "",
      hidden: false, disabled: false, className: "",
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
      remove() { if (node.parentNode) node.parentNode.removeChild(node); node.detached = true; },
      querySelector() { return el(); },
      querySelectorAll() { return []; },
      setSelectionRange() {},
      focus() {}, blur() {}, click() {}, scrollIntoView() {}, requestSubmit() {},
    };
    created.push(node);
    return node;
  }

  const bySel = new Map();
  const document = {
    documentElement: el("html"), body: el("body"), activeElement: null,
    execCalls: [], execResult: true,
    querySelector(sel) {
      if (!bySel.has(sel)) bySel.set(sel, el());
      return bySel.get(sel);
    },
    querySelectorAll() { return []; },
    createElement(tag) { return el(tag); },
    createRange() { return { selectNodeContents() {} }; },
    execCommand(cmd) { document.execCalls.push(cmd); return document.execResult; },
    addEventListener() {}, removeEventListener() {},
  };

  const sandbox = {
    console, document, created,
    isSecureContext: false, navigator: {},
    location: { protocol: "http:", host: "192.168.4.1" },
    localStorage: { s: {}, getItem(k) { return k in this.s ? this.s[k] : null; },
                    setItem(k, v) { this.s[k] = String(v); }, removeItem(k) { delete this.s[k]; } },
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

// ---------- helpers ----------
const $ = (sb, sel) => sb.document.querySelector(sel);
const listHtml = (sb) => $(sb, "#fwList").children.map((c) => c.innerHTML).join("\n");
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };

function stubApi(sb, routes) {
  sb.api = async (p) => {
    for (const [prefix, val] of Object.entries(routes)) {
      if (p === prefix || p.startsWith(prefix)) {
        return typeof val === "function" ? val(p) : val;
      }
    }
    throw new Error("unexpected request " + p);
  };
}

const FILE = (over = {}) => Object.assign({
  name: "granpasso_v4_draft.bin", size: 327680, mtime: 1756000000, desc: true,
  ident: {code: "23ECCLGPSMC", hardware: "5AM X0000", brand: "Moto Morini",
          model: "Granpasso 1200", verified: true, reason: ""},
  mismatch: [],
}, over);

async function loadWith(sb, payload, extraRoutes = {}) {
  stubApi(sb, Object.assign({
    "/api/firmware/desc/": {name: "", text: ""},
    "/api/firmware/check": {level: "ok", reason: "", image: {}, ecu: {}},
    "/api/firmware/catalog": {entries: []},
    "/api/firmware": Object.assign({
      op: "idle", last_op: "", result: "", progress: "", current: "", log: [],
      prog: {percent: -1, done: 0, total: 0}, available: true,
      required_size: 327680, files: [], suggest: null, guard_override: false,
    }, payload),
  }, extraRoutes));
  await sb.loadFirmware();
  await flush();
}

// ---------- tests ----------
let failed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }
function has(hay, needle) {
  assert(String(hay).includes(needle),
    `expected to contain ${JSON.stringify(needle)}, got ${JSON.stringify(String(hay).slice(0, 400))}`);
}

test("the file list shows the code the image claims for itself", async () => {
  const sb = makeSandbox();
  await loadWith(sb, {files: [FILE()]});
  const html = listHtml(sb);
  has(html, "granpasso_v4_draft.bin");
  has(html, "23ECCLGPSMC");
  has(html, "Moto Morini Granpasso 1200");
});

// ---------- the read/write progress bar ----------
const bar = (sb, which) => ({
  box: $(sb, "#fw" + which + "Prog"),
  fill: $(sb, "#fw" + which + "Fill"),
  val: $(sb, "#fw" + which + "Val"),
  track: $(sb, "#fw" + which + "Track"),
});

test("only the bar of the running operation is on screen", async () => {
  const sb = makeSandbox();
  await loadWith(sb, {op: "reading", current: "dump.bin",
                      prog: {percent: 25.0, done: 65536, total: 262144}});
  assert(bar(sb, "Read").box.hidden === false, "the read bar should be visible");
  assert(bar(sb, "Write").box.hidden === true, "the write bar has nothing to show");
  assert(bar(sb, "Read").fill.style.width === "25%", bar(sb, "Read").fill.style.width);
  has(bar(sb, "Read").val.textContent, "25.0 %");
  has(bar(sb, "Read").val.textContent, "64.0 KB / 256.0 KB");
  assert(bar(sb, "Read").track.getAttribute("aria-valuenow") === "25");
});

test("a write draws under its own button", async () => {
  const sb = makeSandbox();
  await loadWith(sb, {op: "writing", current: "img.bin",
                      prog: {percent: 50.0, done: 155652, total: 311304}});
  assert(bar(sb, "Write").box.hidden === false, "the write bar should be visible");
  assert(bar(sb, "Read").box.hidden === true, "the read bar has nothing to show");
  has(bar(sb, "Write").val.textContent, "50.0 %");
});

test("a finished operation keeps its bar until the next one starts", async () => {
  // the board says op:idle the moment the util exits, so `last_op` is what tells
  // the two bars apart once the result is in
  const sb = makeSandbox();
  await loadWith(sb, {op: "idle", last_op: "reading", result: "ok",
                      prog: {percent: 100.0, done: 262144, total: 262144}});
  assert(bar(sb, "Read").box.hidden === false, "a finished read must still show 100 %");
  has(bar(sb, "Read").val.textContent, "100.0 %");
  await loadWith(sb, {op: "idle", last_op: "reading", result: "", prog: {percent: -1}});
  assert(bar(sb, "Read").box.hidden === true, "a cleared result clears the bar");
});

test("a failure keeps the number it died at", async () => {
  const sb = makeSandbox();
  await loadWith(sb, {op: "idle", last_op: "writing", result: "error",
                      prog: {percent: 43.2, done: 134500, total: 311304}});
  assert(bar(sb, "Write").box.hidden === false);
  has(bar(sb, "Write").val.textContent, "43.2 %");
});

test("an operation that never reported a position draws no bar", async () => {
  const sb = makeSandbox();
  await loadWith(sb, {op: "reading", result: "", prog: {percent: -1, done: 0, total: 0}});
  assert(bar(sb, "Read").box.hidden === true, "-1 means the util has said nothing yet");
});

test("a sidecar that contradicts the image is badged", async () => {
  const sb = makeSandbox();
  await loadWith(sb, {files: [FILE({mismatch: ["desc"]})]});
  const html = listHtml(sb);
  has(html, "⚠");
  has(html, sb.window.I18N.en["fw.mismatch.desc"]);
});

test("an unidentified image says so instead of showing nothing", async () => {
  const sb = makeSandbox();
  await loadWith(sb, {files: [FILE({ident: {code: "", reason: "not_5am"}, mismatch: []})]});
  const html = listHtml(sb);
  has(html, sb.window.I18N.en["fw.unknownCode"]);
  has(html, "not_5am");
});

test("an unverified catalogue guess is marked as one", async () => {
  const sb = makeSandbox();
  await loadWith(sb, {files: [FILE({ident: {code: "23ACMCORA", brand: "Moto Morini",
                                            model: "Corsaro 1200", verified: false}})]});
  const html = listHtml(sb);
  has(html, "fw-ident--soft");
  has(html, sb.window.I18N.en["fw.unverified"]);
});

test("a brand typed by a human cannot inject markup", async () => {
  // brand/model are user input from the catalog editor, so this is a real surface
  const sb = makeSandbox();
  await loadWith(sb, {files: [FILE({ident: {code: "X1", brand: "<script>alert(1)</script>",
                                            model: "\"><b>", verified: true}})]});
  const html = listHtml(sb);
  assert(!html.includes("<script>"), "raw <script> reached the DOM");
  has(html, "&lt;script&gt;");
  has(html, "&quot;&gt;&lt;b&gt;");
});

test("the rename offer carries the proposed name and can be dismissed", async () => {
  const sb = makeSandbox();
  await loadWith(sb, {files: [FILE()],
                      suggest: {for: "dump-20260828.bin", name: "23ECCLGPSMC-20260828.bin"}});
  assert($(sb, "#fwSuggest").hidden === false, "the offer should be visible");
  assert($(sb, "#fwSuggestName").value === "23ECCLGPSMC-20260828.bin");
  $(sb, "#fwSuggestNo").handlers.click[0]();
  assert($(sb, "#fwSuggest").hidden === true, "dismissing should hide it");
  // the 1.5 s poll must not bring it back
  await loadWith(sb, {files: [FILE()],
                      suggest: {for: "dump-20260828.bin", name: "23ECCLGPSMC-20260828.bin"}});
  assert($(sb, "#fwSuggest").hidden === true, "a dismissed offer came back");
});

test("a guard warning is shown with both codes before anything is written", async () => {
  const sb = makeSandbox();
  const verdict = {level: "warn", reason: "revision",
                   image: {code: "23ECCLGPSMC"}, ecu: {code: "23ECCLGPSMD"}};
  $(sb, "#fwWriteSelect").value = "granpasso_v4_draft.bin";
  await loadWith(sb, {files: [FILE()]}, {"/api/firmware/check": verdict});
  const box = $(sb, "#fwGuard");
  assert(box.hidden === false, "the verdict should be on screen");
  has(box.textContent, sb.window.I18N.en["fw.guard.revision"]);
  has(box.textContent, "23ECCLGPSMC");
  has(box.textContent, "23ECCLGPSMD");
});

test("a blocking verdict disables the write button", async () => {
  const sb = makeSandbox();
  const verdict = {level: "block", reason: "model_mismatch",
                   image: {code: "23ACMCORA"}, ecu: {code: "23ECCLGPSMD"}};
  $(sb, "#fwWriteSelect").value = "granpasso_v4_draft.bin";
  await loadWith(sb, {files: [FILE()]}, {"/api/firmware/check": verdict});
  assert($(sb, "#fwWriteBtn").disabled === true, "a blocked write must not be clickable");
  has($(sb, "#fwGuard").textContent, sb.window.I18N.en["fw.guard.model_mismatch"]);
});

test("the write confirmation repeats the reason, not just the file name", async () => {
  const sb = makeSandbox();
  const verdict = {level: "warn", reason: "revision",
                   image: {code: "23ECCLGPSMC"}, ecu: {code: "23ECCLGPSMD"}};
  $(sb, "#fwWriteSelect").value = "granpasso_v4_draft.bin";
  await loadWith(sb, {files: [FILE()]}, {"/api/firmware/check": verdict});
  const asked = [];
  sb.confirmDialog = (text) => { asked.push(text); return Promise.resolve(false); };
  await $(sb, "#fwWriteBtn").handlers.click[0]({currentTarget: $(sb, "#fwWriteBtn")});
  await flush();
  has(asked[0], "granpasso_v4_draft.bin");
  has(asked[0], sb.window.I18N.en["fw.guard.revision"]);
});

test("the override banner appears only while the guard is off", async () => {
  const sb = makeSandbox();
  await loadWith(sb, {files: [], guard_override: false});
  assert($(sb, "#fwOverrideActive").hidden === true);
  await loadWith(sb, {files: [], guard_override: true});
  assert($(sb, "#fwOverrideActive").hidden === false, "an armed override must be visible");
});

test("the dump name is proposed from the live identity and then left alone", async () => {
  const sb = makeSandbox();
  const el = $(sb, "#fwReadName");
  sb.proposeReadName("23ECCLGPSMD");
  assert(/^23ECCLGPSMD-\d{8}\.bin$/.test(el.value), "got " + el.value);
  // once the rider types, we stop overwriting the field
  el.handlers.input[0]();
  el.value = "mine.bin";
  sb.proposeReadName("23ECCLGPSMD");
  assert(el.value === "mine.bin", "a hand-typed name was overwritten");
});

test("no live identity means no proposal, not a broken name", async () => {
  const sb = makeSandbox();
  sb.proposeReadName("");
  assert($(sb, "#fwReadName").value === "");
});

const CATALOG = [
  {code: "23ECCLGPSM", space: "image", brand: "Moto Morini", model: "Granpasso 1200",
   verified: true, user: false, revisions: {C: "working base", D: "stock D — baseline"}},
  {code: "23ACMORCORA", space: "image", brand: "Moto Morini", model: "Corsaro 1200",
   verified: false, user: false, guard: false},
  {code: "2237B37SNPZ", space: "image", brand: "Ducati", model: "1098S",
   verified: false, user: false, guard: false},
  {code: "2237BA7LN22", space: "image", brand: "Ducati", model: "1098S",
   verified: false, user: false, guard: false},
  {code: "2229GRS8V68", space: "image", brand: "Moto Guzzi", model: "Griso 1100",
   verified: false, user: false, guard: false},
  {code: "ZZMINE", space: "image", brand: "Mine", model: "", verified: false, user: true},
];

async function withCatalog(sb, entries = CATALOG) {
  stubApi(sb, {"/api/firmware/catalog": {entries}});
  await sb.loadFwCatalog();
  await flush();
  return $(sb, "#fwCatalogList").innerHTML;
}

test("the catalog lists entries and offers delete only for user ones", async () => {
  const sb = makeSandbox();
  const html = await withCatalog(sb);
  has(html, "23ECCLGPSM");
  has(html, 'data-del="ZZMINE"');
  assert(!html.includes('data-del="23ECCLGPSM"'), "a shipped entry must not offer delete");
});

test("the catalog is grouped by manufacturer and then model", async () => {
  const sb = makeSandbox();
  const html = await withCatalog(sb);
  has(html, 'class="fw-cat-brand"');
  has(html, 'class="fw-cat-model"');
  has(html, "<summary>Ducati");
  has(html, "<summary>Moto Morini");
  has(html, "<summary>Granpasso 1200");
  // both 1098S codes belong to one model group, so that group is not repeated
  const models = html.match(/<summary>1098S/g) || [];
  assert(models.length === 1, `1098S should appear once as a group, got ${models.length}`);
  // an entry with no model still lands somewhere rather than vanishing
  has(html, "ZZMINE");
});

test("groups start folded and carry a count", async () => {
  const sb = makeSandbox();
  const html = await withCatalog(sb);
  assert(!/<details class="fw-cat-brand"[^>]* open>/.test(html),
    "nothing should be expanded before the rider asks");
  has(html, 'class="fw-cat-count"');
  has(html, ">2</span>");                       // Ducati holds two codes
  assert($(sb, "#fwCatCount").textContent === "6", $(sb, "#fwCatCount").textContent);
});

test("searching filters by firmware code and opens what it found", async () => {
  const sb = makeSandbox();
  await withCatalog(sb);
  const input = $(sb, "#fwCatSearch");
  input.value = "2237B37";
  input.handlers.input[0]({target: input});
  const html = $(sb, "#fwCatalogList").innerHTML;
  has(html, "2237B37SNPZ");
  assert(!html.includes("2229GRS8V68"), "a non-matching code should be filtered out");
  assert(!html.includes("Moto Guzzi"), "an empty manufacturer group should disappear");
  assert(/<details class="fw-cat-brand"[^>]* open>/.test(html), "matches must be visible");
  assert($(sb, "#fwCatCount").textContent === "1 / 6", $(sb, "#fwCatCount").textContent);
});

test("a full code finds the family entry that covers it", async () => {
  // 23ECCLGPSMD is not an entry of its own — 23ECCLGPSM is, and carries the D
  const sb = makeSandbox();
  await withCatalog(sb);
  const input = $(sb, "#fwCatSearch");
  input.value = "23ECCLGPSMD";
  input.handlers.input[0]({target: input});
  const html = $(sb, "#fwCatalogList").innerHTML;
  has(html, "23ECCLGPSM");
  has(html, "stock D — baseline");
  assert(!html.includes("2237B37SNPZ"), "unrelated codes must be filtered out");
});

test("search is case-insensitive and reports when nothing matches", async () => {
  const sb = makeSandbox();
  await withCatalog(sb);
  const input = $(sb, "#fwCatSearch");
  input.value = "griso";                        // matches nothing: it is a model, not a code
  input.handlers.input[0]({target: input});
  has($(sb, "#fwCatalogList").innerHTML, sb.window.I18N.en["fw.catalogNoMatch"]);
  input.value = "2229grs";
  input.handlers.input[0]({target: input});
  has($(sb, "#fwCatalogList").innerHTML, "2229GRS8V68");
});

test("a manufacturer name from the catalog cannot inject markup", async () => {
  const sb = makeSandbox();
  const html = await withCatalog(sb, [
    {code: "X1", space: "image", brand: "<img src=x onerror=alert(1)>", model: "\"><b>",
     verified: false, user: false},
  ]);
  assert(!html.includes("<img src=x"), "raw markup reached the DOM");
  has(html, "&lt;img src=x");
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok " + name); }
    catch (e) { failed++; console.log("FAIL " + name + ": " + (e && e.message)); }
  }
  process.exit(failed ? 1 : 0);
})();
