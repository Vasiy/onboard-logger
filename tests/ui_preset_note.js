"use strict";
// Headless harness for the preset note on the Logger tab.
//
// A preset says which channels; the note says how to capture them — cold engine,
// before the fan, hold 3000 rpm. It is markdown, which means it is also the one
// place in this UI where text typed by a person becomes markup, so the escaping
// is the test that matters most: mdToHtml() escapes first and builds the markup
// out of the escaped text afterwards. Reversed, a note would be a way to put
// arbitrary HTML into the board's own origin.
//
// Run directly: node tests/ui_preset_note.js  (also driven by test_ui_preset_note.py)

const { makeSandbox: baseSandbox } = require("./ui_sandbox");

const CATALOG = [
  { key: "rpm", name: "RPM", unit: "rpm", group: "known", rli: 1 },
  { key: "coolant_t", name: "Engine temperature", unit: "°C", group: "known", rli: 2 },
  { key: "vbat", name: "Battery", unit: "V", group: "known", rli: 3 },
];

const PRESETS = [
  { name: "Cold", keys: ["rpm", "coolant_t"], note: "## Cold start\n- fan off\n- below 40" },
  { name: "Adv", keys: ["vbat"], note: "" },
  { name: "", keys: [], note: "" },
];

function snap(over) {
  return Object.assign({
    status: "connected", values: {}, catalog: CATALOG,
    selected: ["rpm", "coolant_t"],
    presets: PRESETS.map((p) => ({ ...p })), free_note: "hand-picked note",
    log_decoded_file: "", log_raw_file: "", storage: {}, wifi: {}, dtc: [],
  }, over || {});
}

// paramMode is read out of localStorage once, at eval time, so the mode is
// seeded rather than switched: the buttons that change it live behind a
// document.querySelectorAll the stub answers with nothing.
function makeSandbox(mode, over) {
  const posts = [];
  const sb = baseSandbox({
    storage: { paramMode: mode || "view" },
    fetch(url, opts) {
      if (opts && opts.method === "POST") {
        posts.push([url, opts.body && JSON.parse(opts.body)]);
        return Promise.resolve({ ok: true, status: 200,
          json: () => Promise.resolve(JSON.parse(opts.body)) });
      }
      const body = url === "/api/config" ? { logging: {}, diag: {} }
        : url === "/api/addons" ? { addons: [] } : { files: [] };
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
    },
  });
  sb.posts = posts;
  // applySnapshot() locks the log switches through closest(".rec-row"); on the
  // stub every node hangs off body, so that ancestor has to be built here
  for (const sel of ["#decToggle", "#rawToggle"]) {
    const row = sb.document.createElement("div");
    row.className = "rec-row";
    row.appendChild(sb.document.querySelector(sel));
  }
  sb.window.applySnapshot(snap(over));
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
function hasNot(hay, needle) {
  assert(!String(hay).includes(needle),
    `expected NOT to contain ${JSON.stringify(needle)}, got ${JSON.stringify(hay)}`);
}

const md = (sb, src) => sb.window.mdToHtml(src);
const box = (sb) => sb.document.querySelector("#presetNote");
const view = (sb) => sb.document.querySelector("#presetNoteView");
const edit = (sb) => sb.document.querySelector("#presetNoteEdit");

/* ---------- the renderer ---------- */

test("markup in a note comes out as text, not as markup", () => {
  const sb = makeSandbox("view");
  const bad = '<script>alert(1)</script><img src=x onerror="alert(2)">';
  const out = md(sb, bad);
  hasNot(out, "<script");
  hasNot(out, "<img");
  hasNot(out, "onerror=\"");
  has(out, "&lt;script&gt;");
  has(out, "&lt;img src=x onerror=&quot;alert(2)&quot;&gt;");
});

test("bold and code cannot smuggle a tag in either", () => {
  const sb = makeSandbox("view");
  // the inline pass runs on already-escaped text, so the < is long gone
  has(md(sb, "**<b>hi</b>**"), "<strong>&lt;b&gt;hi&lt;/b&gt;</strong>");
  has(md(sb, "`<i>x</i>`"), "<code>&lt;i&gt;x&lt;/i&gt;</code>");
});

test("the subset renders", () => {
  const sb = makeSandbox("view");
  eq(md(sb, "## Cold start"), "<h4>Cold start</h4>");
  eq(md(sb, "- one\n- two"), "<ul><li>one</li><li>two</li></ul>");
  eq(md(sb, "1. first\n2. second"), "<ol><li>first</li><li>second</li></ol>");
  eq(md(sb, "---"), "<hr>");
  eq(md(sb, "plain"), "<p>plain</p>");
  has(md(sb, "*it*"), "<em>it</em>");
});

test("a list ends where the blank line is, and a paragraph follows", () => {
  const sb = makeSandbox("view");
  eq(md(sb, "- a\n\nafter"), "<ul><li>a</li></ul><p>after</p>");
});

test("no links: the bike has nowhere to go", () => {
  const sb = makeSandbox("view");
  hasNot(md(sb, "[click](http://example.com)"), "<a ");
});

/* ---------- the block ---------- */

test("read mode renders the lit preset's note and cannot be edited", () => {
  const sb = makeSandbox("view");      // selection equals the first preset
  eq(box(sb).hidden, false);
  eq(edit(sb).hidden, true, "no textarea in read mode");
  has(view(sb).innerHTML, "<h4>Cold start</h4>");
  has(view(sb).innerHTML, "<li>fan off</li>");
});

test("a preset with no note hides the whole block in read mode", () => {
  const sb = makeSandbox("view", { selected: ["vbat"] });   // preset 2, note ""
  eq(box(sb).hidden, true, "an empty box would just take up the card");
});

test("a set matching no preset falls back to the free note", () => {
  const sb = makeSandbox("view", { selected: ["rpm", "vbat"] });
  eq(box(sb).hidden, false);
  has(view(sb).innerHTML, "hand-picked note");
  eq(sb.document.querySelector("#presetNoteWho").textContent,
     sb.window.I18N.en["note.free"]);
});

test("pick mode offers the textarea even when the note is empty", () => {
  const sb = makeSandbox("edit", { selected: ["vbat"] });
  eq(box(sb).hidden, false, "otherwise there is nowhere to write the first note");
  eq(edit(sb).hidden, false);
  eq(view(sb).hidden, true);
});

test("typing goes out on blur, not per keystroke, and carries the free note", () => {
  const sb = makeSandbox("edit", { selected: ["rpm", "vbat"] });   // no preset lit
  const ed = edit(sb);
  ed.value = "hold **3000 rpm**";
  ed.handlers.input.forEach((fn) => fn({ target: ed }));
  eq(sb.posts.length, 0, "nothing went out while typing");
  ed.handlers.change.forEach((fn) => fn({ target: ed }));
  eq(sb.posts.length, 1, "one write on blur");
  eq(sb.posts[0][0], "/api/presets");
  eq(sb.posts[0][1].free_note, "hold **3000 rpm**");
});

test("with a slot open the note belongs to that slot, not to the live set", () => {
  // the field follows the same slot as the name and the ticks — one rule for all
  const sb = makeSandbox("edit");           // the live set is preset 0
  sb.window.onPresetClick(1);               // open slot 1 for editing
  const ed = edit(sb);
  ed.value = "belongs to Adv";
  ed.handlers.input.forEach((fn) => fn({ target: ed }));
  ed.handlers.change.forEach((fn) => fn({ target: ed }));
  const sent = sb.posts[sb.posts.length - 1][1];
  eq(sent.presets[1].note, "belongs to Adv");
  eq(sent.presets[0].note, PRESETS[0].note, "the other slot is untouched");
});

test("the 0.2 s push does not eat what is being typed", () => {
  const sb = makeSandbox("edit");
  const ed = edit(sb);
  ed.value = "half-typed";
  sb.document.activeElement = ed;
  sb.window.applySnapshot(snap());          // the board pushes its own copy
  eq(ed.value, "half-typed", "the snapshot overwrote the field");
});

test("the counter says what is left of the limit", () => {
  const sb = makeSandbox("edit", { selected: ["rpm", "vbat"] });
  const ed = edit(sb);
  ed.value = "12345";
  ed.handlers.input.forEach((fn) => fn({ target: ed }));
  eq(sb.document.querySelector("#presetNoteCount").textContent, String(4096 - 5));
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok " + name); }
    catch (e) { failed++; console.log("FAIL " + name + "\n   " + e.message); }
  }
  console.log(`\n${tests.length - failed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
