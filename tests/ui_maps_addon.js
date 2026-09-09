"use strict";
// Headless harness for the add-on wiring in the browser.
//
// Two things live here. On the Firmware tab, "Show map" hands the files the
// rider ticked to the map viewer add-on — and it must reuse the tab it already
// opened, because a rider comparing calibrations presses it over and over and
// would otherwise be closing windows instead of reading maps. In Config ->
// System, the panel lists what is installed.
//
// The button is only there when an add-on can draw a firmware image; a board
// without one must not show a control that goes nowhere.
//
// Run directly: node tests/ui_maps_addon.js   (also driven by test_ui_maps_addon.py)

const { makeSandbox: baseSandbox } = require("./ui_sandbox");

const MAPS = {
  name: "maps", version: "0.3.0", title: "ECU map viewer",
  url: "/addons/maps/", files: 2,
};

function makeSandbox(addons = [MAPS]) {
  const opened = [];
  const sb = baseSandbox({
    fetch(url) {
      const body = url === "/api/addons" ? { addons: sb.addons }
        : url === "/api/config" ? { logging: {}, diag: {} }
        : url === "/api/firmware" ? { op: "idle", files: [], available: true }
        : { files: [] };
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
    },
  });
  sb.addons = addons;
  sb.opened = opened;
  // window.open is the whole point of the button, so it is recorded rather than
  // stubbed away; the returned handle only has to answer focus()
  sb.open = (url, target) => { opened.push([url, target]); return { focus() {} }; };
  // The card list is built with innerHTML, so its checkboxes are not nodes on
  // this stub. fwChecked() reads them through this one selector, so that is
  // what the harness answers -- the button's own logic is what is under test.
  sb.selected = [];
  sb.document.querySelectorAll = (sel) =>
    sel === "#fwList .fwsel:checked" ? sb.selected.map((v) => ({ value: v })) : [];
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

const btn = (sb) => sb.document.querySelector("#fwMapBtn");
const press = (sb) => btn(sb).handlers.click[0]({ currentTarget: btn(sb), target: btn(sb) });

test("with no add-on installed the button is not there at all", async () => {
  const sb = makeSandbox([]);
  await sb.window.loadAddons();
  eq(btn(sb).hidden, true, "the button should stay hidden");
  has(sb.document.querySelector("#addonList").innerHTML, sb.window.I18N.en["addon.none"]);
});

test("an add-on that is not the map viewer does not put the button back", async () => {
  const sb = makeSandbox([{ name: "notes", version: "1", title: "Notes",
                            url: "/addons/notes/", files: 0 }]);
  await sb.window.loadAddons();
  eq(btn(sb).hidden, true, "only an add-on that can draw a map earns the button");
});

test("the selected images are handed over in the URL", async () => {
  const sb = makeSandbox();
  await sb.window.loadAddons();
  eq(btn(sb).hidden, false, "the button appears once the add-on is installed");
  sb.selected = ["stock.bin", "stage2.bin"];
  press(sb);
  eq(sb.opened.length, 1, "one window");
  eq(sb.opened[0][0], "/addons/maps/?bin=stock.bin&bin=stage2.bin", "url");
});

test("a name with a space or an ampersand survives the trip", async () => {
  const sb = makeSandbox();
  await sb.window.loadAddons();
  sb.selected = ["my map&1.bin"];
  press(sb);
  eq(sb.opened[0][0], "/addons/maps/?bin=my%20map%261.bin", "url");
});

test("pressing again lands in the tab that is already open", async () => {
  const sb = makeSandbox();
  await sb.window.loadAddons();
  sb.selected = ["a.bin"];
  press(sb);
  sb.selected = ["b.bin", "c.bin"];
  press(sb);
  eq(sb.opened.length, 2, "two presses");
  // the target name is what makes the second one reuse the first one's tab
  eq(sb.opened[0][1], "ecu-map-viewer", "first target");
  eq(sb.opened[1][1], "ecu-map-viewer", "second target");
  assert(sb.opened[0][0] !== sb.opened[1][0], "the second press carries the new selection");
});

test("nothing selected opens nothing, and says why", async () => {
  const sb = makeSandbox();
  await sb.window.loadAddons();
  sb.selected = [];
  press(sb);
  eq(sb.opened.length, 0, "no window");
  const said = sb.document.querySelector("#fwDiffResult").textContent;
  eq(said, sb.window.I18N.en["fw.selectOne"], "the reason is translated");
});

test("the panel names the add-on and what it is holding", async () => {
  const sb = makeSandbox();
  await sb.window.loadAddons();
  const html = sb.document.querySelector("#addonList").children.map((c) => c.innerHTML).join("");
  has(html, "ECU map viewer");
  has(html, "v0.3.0");
  has(html, "/addons/maps/");
  has(html, sb.window.I18N.en["addon.files"]);
  has(html, 'data-rm="maps"');
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok " + name); }
    catch (e) { failed++; console.log("FAIL " + name + "\n   " + e.message); }
  }
  console.log(`\n${tests.length - failed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
