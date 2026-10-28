const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

test("renderer route notifications are not echoed as navigation commands", () => {
  const source = fs.readFileSync(path.join(__dirname, "../main.js"), "utf8");
  const start = source.indexOf('ipcMain.on("olivia:native-invoke"');
  const end = source.indexOf('ipcMain.handle("olivia:request"', start);
  let invoke;
  const sent = [];
  vm.runInNewContext(source.slice(start, end), {
    ipcMain: { on: (_name, handler) => { invoke = handler; } },
    fromApp() {},
    mainWindow: { webContents: { send: (...args) => sent.push(args) } },
  });
  invoke({}, "updateRoute", { route: "studio" });
  assert.deepEqual(sent, []);
  invoke({}, "collectionStop", {});
  assert.equal(sent.length, 1);
  assert.equal(sent[0][1], "collectionStop");
});

test("native navigation preserves the private frontend entry and its API referrer", () => {
  const entry = "http://127.0.0.1:49152/private-test-entry/index.html";
  const location = new URL(entry + "#/");
  const handlers = new Map();
  const events = [];
  const context = {
    require: () => ({
      contextBridge: { exposeInMainWorld() {} },
      ipcRenderer: { sendSync() {}, on: (name, handler) => handlers.set(name, handler) },
    }),
    window: {
      location,
      history: { pushState: (_state, _title, url) => { location.href = new URL(url, location).href; } },
      dispatchEvent: (event) => events.push(event.type),
    },
    PopStateEvent: class { constructor(type) { this.type = type; } },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../preload.js"), "utf8"), context);
  for (const route of ["studio", "/settings", "/letter", "//example.com"]) {
    handlers.get("olivia:native-action")({}, "updateRoute", { route });
    assert.equal(location.origin + location.pathname, entry);
    assert.equal(location.hash, "#" + (route.startsWith("/") ? route : "/" + route));
  }
  assert.deepEqual(events, ["popstate", "popstate", "popstate", "popstate"]);
});
