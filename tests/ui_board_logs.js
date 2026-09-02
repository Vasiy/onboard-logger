"use strict";
// Headless harness for the two log browsers: the Logs tab and Config -> System.
//
// Both come out of the same makeLogBrowser() factory, so the thing worth testing
// is that the System one really is the Logs one — day groups, a kind filter, an
// auto-archive switch, a download link per row — over a different slice of
// /api/logs. app/static/app.js is evaluated whole in a vm on a DOM stub and the
// browsers are driven through their own refresh/filter handlers, exactly as a
// tap does. The `const` instances are invisible from here (top-level const does
// not land on the vm global), which is why nothing reaches into them directly.
//
// Run directly: node tests/ui_board_logs.js   (also driven by test_ui_board_logs.py)

const { makeSandbox: baseSandbox, read } = require("./ui_sandbox");

const FILES = [
  { name: "01-09-2026/diag-20260901-100000.log", file: "diag-20260901-100000.log",
    day: "01-09-2026", size: 4096, mtime: 1788000000, kind: "diag", zip: false },
  { name: "01-09-2026/fw-reading-20260901-204455.log", file: "fw-reading-20260901-204455.log",
    day: "01-09-2026", size: 1389, mtime: 1787999000, kind: "fw", zip: false },
  { name: "31-08-2026/diag-20260831-090000.log.zip", file: "diag-20260831-090000.log.zip",
    day: "31-08-2026", size: 900, mtime: 1787900000, kind: "diag", zip: true },
];
const RIDE = [
  { name: "31-08-2026/kline-dec-1.csv", file: "kline-dec-1.csv", day: "31-08-2026",
    size: 10, mtime: 1787900001, kind: "decoded", zip: false },
];

// The board answers the two slices and /api/config; POSTs are recorded so the
// auto-archive switch can be checked against the key it is supposed to write.
function makeSandbox() {
  const posts = [];
  const sb = baseSandbox({
    fetch(url, opts) {
      if (opts && opts.method === "POST") posts.push([url, JSON.parse(opts.body)]);
      const body = url.includes("kind=board") ? { files: FILES }
        : url.includes("kind=ride") ? { files: RIDE, dest: "internal", root: "/root/k-line" }
        : url === "/api/config" ? { logging: { zip_after: false }, diag: { zip_after: true } }
        : {};
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
    },
  });
  sb.posts = posts;
  return sb;
}

let failed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }
function has(hay, needle) {
  assert(String(hay).includes(needle), `expected to contain ${JSON.stringify(needle)}, got ${JSON.stringify(hay)}`);
}
const fire = async (sb, sel, ev = "click", arg = {}) => {
  const n = sb.document.querySelector(sel);
  const fn = n.handlers[ev] && n.handlers[ev][0];
  assert(fn, `${sel} has no ${ev} handler`);
  await fn({ ...arg, currentTarget: n, target: arg.target || n });
  return n;
};
// the list renders day headers as elements and rows as innerHTML inside them
const listHtml = (sb, sel) =>
  sb.document.querySelector(sel).children.map((c) => c.innerHTML +
    c.children.map((r) => r.innerHTML).join("")).join("\n");
const dayLabels = (sb, sel) =>
  sb.document.querySelector(sel).children
    .flatMap((c) => c.children.map((x) => x.textContent)).filter(Boolean);

test("the System list groups by day, newest first, and folds the older days", async () => {
  const sb = makeSandbox();
  await fire(sb, "#boardLogsRefresh");
  const box = sb.document.querySelector("#boardLogsList");
  const groups = box.children.filter((c) => c.classList.contains("log-group"));
  const wraps = box.children.filter((c) => c.classList.contains("log-group-wrap"));
  assert(groups.length === 2, "two days expected, got " + groups.length);
  const labels = dayLabels(sb, "#boardLogsList");
  has(labels[0], "01-09-2026");
  has(labels[1], "31-08-2026");
  assert(wraps[0].hidden === false, "the newest day starts open");
  assert(wraps[1].hidden === true, "older days start folded");
  has(labels[0], "2 " + sb.window.I18N.en["logs.groupCount"]);
});

test("both kinds are badged and only the System list carries them", async () => {
  const sb = makeSandbox();
  await fire(sb, "#boardLogsRefresh");
  const html = listHtml(sb, "#boardLogsList");
  has(html, ">deb<");
  has(html, ">fw<");
  has(html, "diag-20260901-100000.log");
  has(html, "fw-reading-20260901-204455.log");
  await fire(sb, "#logsRefresh");
  const ride = listHtml(sb, "#logsList");
  has(ride, "kline-dec-1.csv");
  assert(!ride.includes("diag-"), "a diagnostics file must not reach the Logs tab");
  assert(!ride.includes("fw-reading"), "a firmware log must not reach the Logs tab");
});

test("the kind filter narrows the System list without touching the Logs tab", async () => {
  const sb = makeSandbox();
  await fire(sb, "#boardLogsRefresh");
  await fire(sb, "#logsRefresh");
  const btn = { closest: () => btn, dataset: { f: "fw" }, classList: { toggle() {} } };
  await fire(sb, "#boardLogFilter", "click", { target: { closest: () => btn } });
  const html = listHtml(sb, "#boardLogsList");
  has(html, "fw-reading-20260901-204455.log");
  assert(!html.includes("diag-2026"), "kind=fw must hide the diagnostics files");
  has(listHtml(sb, "#logsList"), "kline-dec-1.csv");   // the other browser is untouched
});

test("every row offers a download; only a plain file is also a link to its text", async () => {
  const sb = makeSandbox();
  await fire(sb, "#boardLogsRefresh");
  const html = listHtml(sb, "#boardLogsList");
  has(html, "/api/diag.txt?file=");
  has(html, "/api/firmware/log.txt?file=");
  has(html, "01-09-2026%2Fdiag-20260901-100000.log");  // the day folder survives the URL
  const zipRow = html.split("\n").find((l) => l.includes("diag-20260831-090000.log.zip"));
  assert(zipRow, "the archive should still be listed");
  assert(!zipRow.includes("<a href=\"/api/diag.txt"), "an archive has no text view");
  has(zipRow, "badge zip");
});

test("the auto-archive switch writes diag.zip_after, the Logs one logging.zip_after", async () => {
  const sb = makeSandbox();
  await fire(sb, "#boardLogsRefresh");
  assert(sb.document.querySelector("#boardLogZip").checked === true,
         "the checkbox reflects diag.zip_after from /api/config");
  await fire(sb, "#boardLogZip", "change", { target: { checked: true } });
  await fire(sb, "#logZip", "change", { target: { checked: true } });
  const cfgPosts = sb.posts.filter(([u]) => u === "/api/config").map(([, b]) => b);
  assert(JSON.stringify(cfgPosts[0]) === '{"diag":{"zip_after":true}}', JSON.stringify(cfgPosts[0]));
  assert(JSON.stringify(cfgPosts[1]) === '{"logging":{"zip_after":true}}', JSON.stringify(cfgPosts[1]));
});

test("the System block is markup, not injected by script", () => {
  const html = read("app/static/index.html");
  for (const id of ["boardLogsList", "boardLogFilter", "boardLogSort", "boardLogZip",
                    "boardLogsDownloadBtn", "boardLogsDeleteBtn", "boardLogsRefresh"]) {
    has(html, `id="${id}"`);
  }
  has(html, 'data-f="diag"');
  has(html, 'data-f="fw"');
});

test("every locale names the board-log panel", () => {
  const sb = makeSandbox();
  for (const [loc, table] of Object.entries(sb.window.I18N)) {
    for (const key of ["cfg.boardLogs", "cfg.boardLogsEmpty", "cfg.boardLogsHint",
                       "cfg.boardZip", "cfg.kindDiag", "cfg.kindFw"]) {
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
