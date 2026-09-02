"use strict";
// Shared DOM stub for the headless UI harnesses.
//
// app/static/app.js is a plain script with no exports, so it is evaluated whole
// inside a vm context on this stub: its top-level `function` declarations land
// on that context's global and can be called from a harness. (`let`/`const` at
// the top level do not — assertions have to go through the functions.)
//
// makeSandbox({ fetch }) lets a harness answer the app's own API calls.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const read = (p) => fs.readFileSync(path.join(ROOT, p), "utf8");

process.on("unhandledRejection", () => {});   // init()'s fetches have nowhere to go here

// ---------- DOM stub ----------
function matchesSel(node, sel) {
  const m = String(sel).trim()
    .match(/^([a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)((?:\[[^\]]+\])*)$/);
  if (!m) return false;                       // compound selectors: not our business
  const [, tag, cls, attrs] = m;
  if (tag && node.tagName !== tag) return false;
  for (const c of (cls || "").match(/[.#][\w-]+/g) || []) {
    if (c[0] === ".") { if (!node.classList.contains(c.slice(1))) return false; }
    else if (node.attrs.id !== c.slice(1)) return false;
  }
  for (const a of (attrs || "").match(/\[[^\]]+\]/g) || []) {
    const i = a.indexOf("=");
    const k = (i < 0 ? a.slice(1, -1) : a.slice(1, i)).trim();
    const want = i < 0 ? undefined : a.slice(i + 1, -1).replace(/^["']|["']$/g, "");
    let have = node.attrs[k];
    if (have === undefined && k.startsWith("data-")) {
      have = node.dataset[k.slice(5).replace(/-(\w)/g, (_, ch) => ch.toUpperCase())];
    }
    if (want === undefined ? have === undefined : String(have) !== want) return false;
  }
  return true;
}

function makeSandbox(opts = {}) {
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
      _cls: "",
      textContent: "",
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
      // Real descendants first, so a list built with createElement/appendChild
      // can be walked the way the browser walks it. Falling back to one stable
      // node per selector keeps confirmDialog working: its buttons come from
      // innerHTML, so they are not nodes here, yet the code queries them twice
      // and expects the same element both times.
      _bySel: null,
      _descendants(out = []) {
        for (const c of this.children) { out.push(c); if (c._descendants) c._descendants(out); }
        return out;
      },
      querySelectorAll(sel) {
        if (!sel) return [];
        return this._descendants().filter((n) => matchesSel(n, sel));
      },
      querySelector(sel) {
        const hit = this.querySelectorAll(sel)[0];
        if (hit) return hit;
        this._bySel ||= new Map();
        if (!this._bySel.has(sel)) this._bySel.set(sel, el());
        return this._bySel.get(sel);
      },
      closest(sel) {
        let n = this;
        while (n) { if (matchesSel(n, sel)) return n; n = n.parentNode; }
        return null;
      },
      setSelectionRange(a, b) { node.selectedRange = [a, b]; },
      focus() {}, blur() {}, click() {}, scrollIntoView() {}, requestSubmit() {},
    };
    // clearing a list is `innerHTML = ""`, so the children have to go with it —
    // otherwise a re-render reads as the old rows plus the new ones
    let html = "";
    Object.defineProperty(node, "innerHTML", {
      get() { return html; },
      set(v) { html = String(v); if (html === "") node.children = []; },
    });
    Object.defineProperty(node, "nextElementSibling", {
      get() {
        const sibs = node.parentNode ? node.parentNode.children : [];
        return sibs[sibs.indexOf(node) + 1] || null;
      },
    });
    Object.defineProperty(node, "className", {
      get() { return [...node.classList._s].join(" "); },
      set(v) {
        node.classList._s = new Set(String(v).split(/\s+/).filter(Boolean));
      },
    });
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
    fetch: opts.fetch || (() => Promise.reject(new Error("offline"))),
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

module.exports = { makeSandbox, read, ROOT };
