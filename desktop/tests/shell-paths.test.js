const assert = require("node:assert/strict");
const { test } = require("node:test");
const { isBundlePath, openDecision } = require("../shell-paths");

test("bundle directories are revealed, never opened", () => {
  for (const target of ["/data/Evil.app", "/data/Evil.APP", "/data/Evil.app/Contents", "/data/x.bundle", "/data/y.plugin"]) {
    assert.equal(isBundlePath(target), true, target);
    assert.equal(openDecision("openDirectory", target, true), "reveal", target);
  }
});

test("plain directories open and files or reveal requests are revealed", () => {
  assert.equal(isBundlePath("/data/diagnostics"), false);
  assert.equal(openDecision("openDirectory", "/data/diagnostics", true), "open");
  assert.equal(openDecision("openDirectory", "/data/song.mp3", false), "reveal");
  assert.equal(openDecision("revealFileInExplorer", "/data/diagnostics", true), "reveal");
});
