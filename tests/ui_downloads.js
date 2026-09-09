"use strict";
// Headless harness for the download and file-picking paths.
//
// Safari on iOS ignores the download attribute on a synthesized anchor and
// refuses to save a blob: URL, so the old UI handed an iPhone nothing at all --
// and where an anchor did work, a loop over several delivered only the first.
// What is tested here is that nothing builds an anchor or a blob any more: the
// browser is sent to a URL and the board answers with Content-Disposition.
//
// The picker is the other half. It carries no accept filter, because iOS
// resolves extensions through UTIs and greys out every .bin under one, so the
// extension has to be refused in JS instead.
//
// Run directly: node tests/ui_downloads.js   (also driven by test_ui_downloads.py)

const { makeSandbox: baseSandbox } = require("./ui_sandbox");

const DAY = "07-09-2026";
const ROWS = [
  { name: DAY + "/kline-dec-1.csv", file: "kline-dec-1.csv", size: 10, mtime: 2, kind: "dec" },
  { name: DAY + "/kline-dec-2.csv", file: "kline-dec-2.csv", size: 10, mtime: 3, kind: "dec" },
];

function makeSandbox() {
  const sb = baseSandbox({
    fetch(url) {
      const body = url.startsWith("/api/logs") ? { files: ROWS }
        : url === "/api/config" ? { logging: {}, diag: {} }
        : url === "/api/addons" ? { addons: [] }
        : url === "/api/firmware" ? { op: "idle", files: [], available: true }
        : { files: [] };
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
    },
  });
  // every way a page can hand a file over, recorded rather than performed
  sb.navigated = [];
  sb.anchors = [];
  sb.blobs = [];
  Object.defineProperty(sb.location, "href", {
    set(v) { sb.navigated.push(v); },
    get() { return "http://192.168.4.1/"; },
  });
  sb.URL = {
    createObjectURL: (b) => { sb.blobs.push(b); return "blob:fake"; },
    revokeObjectURL() {},
  };
  const create = sb.document.createElement.bind(sb.document);
  sb.document.createElement = (tag) => {
    const n = create(tag);
    if (tag === "a") sb.anchors.push(n);
    return n;
  };
  return sb;
}

let failed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }
function eq(got, want, msg) {
  assert(String(got) === String(want),
    (msg || "mismatch") + `: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}
function has(hay, needle) {
  assert(String(hay).includes(needle),
    `expected to contain ${JSON.stringify(needle)}, got ${JSON.stringify(hay)}`);
}

const fire = (sb, sel, type, ev) => {
  const n = sb.document.querySelector(sel);
  (n.handlers[type] || []).forEach((fn) => fn(ev || { currentTarget: n, target: n }));
};

test("several firmware images leave as one navigation, not a pile of anchors", async () => {
  const sb = makeSandbox();
  sb.selected = ["stock.bin", "tuned.bin"];
  sb.document.querySelectorAll = (s) =>
    s === "#fwList .fwsel:checked" ? sb.selected.map((v) => ({ value: v })) : [];
  fire(sb, "#fwDownloadBtn", "click");
  eq(sb.navigated.length, 1, "one navigation");
  has(sb.navigated[0], "/api/firmware/download?names=stock.bin%2Ctuned.bin");
  eq(sb.anchors.length, 0, "no <a download> was built");
});

test("a single image goes the same way", async () => {
  const sb = makeSandbox();
  sb.selected = ["stock.bin"];
  sb.document.querySelectorAll = (s) =>
    s === "#fwList .fwsel:checked" ? sb.selected.map((v) => ({ value: v })) : [];
  fire(sb, "#fwDownloadBtn", "click");
  eq(sb.navigated.length, 1);
  has(sb.navigated[0], "names=stock.bin");
});

test("nothing selected navigates nowhere", async () => {
  const sb = makeSandbox();
  sb.document.querySelectorAll = () => [];
  fire(sb, "#fwDownloadBtn", "click");
  eq(sb.navigated.length, 0, "no navigation");
  eq(sb.document.querySelector("#fwDiffResult").textContent,
     sb.window.I18N.en["fw.selectOne"]);
});

test("a log bundle is a navigation too, and never a blob", async () => {
  const sb = makeSandbox();
  // makeLogBrowser is built behind a top-level const, so it is not reachable
  // on the vm global; the rows are fed through the selector it reads instead
  sb.document.querySelectorAll = (s) =>
    s.includes("logsel") ? ROWS.map((r) => ({ value: r.name, checked: true })) : [];
  fire(sb, "#logsDownloadBtn", "click");
  await new Promise((r) => setImmediate(r));
  eq(sb.blobs.length, 0, "no blob: URL was created");
  eq(sb.anchors.length, 0, "no <a download> was built");
  eq(sb.navigated.length, 1, "one navigation");
  has(sb.navigated[0], "/api/logs/download?names=");
  has(sb.navigated[0], "zipname=");
});

test("the pickers carry no accept filter", async () => {
  const html = require("fs").readFileSync(
    require("path").join(__dirname, "..", "app/static/index.html"), "utf8");
  const inputs = html.match(/<input[^>]*type="file"[^>]*>/g) || [];
  assert(inputs.length >= 3, "expected the three upload inputs, got " + inputs.length);
  inputs.forEach((tag) => {
    assert(!/accept=/.test(tag), "iOS greys out .bin under an accept filter: " + tag);
    assert(!/\bhidden\b/.test(tag), "display:none is what iOS will not open: " + tag);
    assert(/class="file-pick"/.test(tag), "the input must stay rendered: " + tag);
  });
});

test("a wrong extension is refused in JS instead of being uploaded", async () => {
  const sb = makeSandbox();
  // init() posts the browser clock at boot, so only the upload is counted
  const posted = [];
  sb.fetch = (url, opts) => {
    if (opts && opts.method === "POST" && url.includes("/upload")) posted.push(url);
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  };
  const input = sb.document.querySelector("#fwUpload");
  input.files = [{ name: "notes.txt" }];
  fire(sb, "#fwUpload", "change", { currentTarget: input, target: input });
  await new Promise((r) => setImmediate(r));
  eq(posted.length, 0, "nothing was uploaded");
  eq(input.value, "", "the input is cleared either way");
  const said = sb.document.querySelector("#toasts").children.map((n) => n.textContent);
  assert(said.some((x) => x.includes(sb.window.I18N.en["files.wrongType"])),
         "the reason is shown: " + said.join(" | "));
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok " + name); }
    catch (e) { failed++; console.log("FAIL " + name + "\n   " + e.message); }
  }
  console.log(`\n${tests.length - failed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
