const assert = require("node:assert/strict");
const { test } = require("node:test");
const { availableMacRelease } = require("../mac-updates");

test("selects a newer DMG for the running architecture", () => {
  const releases = [
    { tag_name: "v2.4.13", assets: [{ name: "BSide Olivia-2.4.13-x64.dmg", browser_download_url: "https://github.com/Ornn8/bside-olivia-community/releases/download/v2.4.13/intel.dmg" }] },
    { tag_name: "v2.4.12", assets: [{ name: "BSide Olivia-2.4.12-arm64.dmg", browser_download_url: "https://github.com/Ornn8/bside-olivia-community/releases/download/v2.4.12/arm.dmg" }] },
  ];
  assert.equal(availableMacRelease(releases, "2.4.11", "arm64")?.version, "2.4.12");
  assert.equal(availableMacRelease(releases, "2.4.11", "x64")?.version, "2.4.13");
  assert.equal(availableMacRelease(releases, "2.4.13", "x64"), null);
});

test("rejects a release asset outside the project", () => {
  assert.equal(availableMacRelease([{
    tag_name: "v2.4.12", assets: [{ name: "BSide Olivia-2.4.12-arm64.dmg", browser_download_url: "https://elsewhere.test/download.dmg" }],
  }], "2.4.11", "arm64"), null);
});
