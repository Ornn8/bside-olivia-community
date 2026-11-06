const assert = require("node:assert/strict");
const { test } = require("node:test");
const trayIcon = require("../tray-icon");

test("bundled menu bar template is a non-empty 2x PNG", () => {
  const bytes = trayIcon;
  assert.deepEqual(bytes.subarray(0, 8), Buffer.from("89504e470d0a1a0a", "hex"));
  assert.equal(bytes.readUInt32BE(16), 36);
  assert.equal(bytes.readUInt32BE(20), 36);
  assert.ok(bytes.length > 100);
});
