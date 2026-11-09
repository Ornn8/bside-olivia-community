const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");
const { REQUIRED_ORIGINAL_ASSETS, hasOriginalAssets, copyOriginalAssets } = require("../original-assets");

test("authorized original assets are copied into a private destination", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "olivia-assets-"));
  const source = path.join(root, "source");
  const destination = path.join(root, "Original");
  fs.mkdirSync(source);
  for (const name of REQUIRED_ORIGINAL_ASSETS) fs.writeFileSync(path.join(source, name), name);
  assert.equal(hasOriginalAssets(source), true);
  copyOriginalAssets(source, destination);
  assert.equal(hasOriginalAssets(destination), true);
  assert.equal(fs.statSync(path.join(destination, "feapp.dat")).mode & 0o777, 0o600);
  fs.rmSync(root, { recursive: true, force: true });
});

test("incomplete original asset directories are rejected", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "olivia-assets-"));
  assert.equal(hasOriginalAssets(root), false);
  assert.throws(() => copyOriginalAssets(root, path.join(root, "dest")), /ORIGINAL_FRONTEND_ASSETS_INVALID/);
  fs.rmSync(root, { recursive: true, force: true });
});
