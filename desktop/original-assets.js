const fs = require("fs");
const path = require("path");

const REQUIRED_ORIGINAL_ASSETS = Object.freeze([
  "feapp.dat",
  "webplayer.dat",
  "NutStudioUI.dll",
  "NutContainerPlugin.dll",
  "native-navigation-compatibility.json",
]);

function hasOriginalAssets(root) {
  if (typeof root !== "string" || !path.isAbsolute(root)) return false;
  return REQUIRED_ORIGINAL_ASSETS.every((name) => {
    try { return fs.statSync(path.join(root, name)).isFile(); }
    catch { return false; }
  });
}

function copyOriginalAssets(source, destination) {
  if (!hasOriginalAssets(source)) throw new Error("ORIGINAL_FRONTEND_ASSETS_INVALID");
  fs.mkdirSync(destination, { recursive: true, mode: 0o700 });
  for (const name of REQUIRED_ORIGINAL_ASSETS) {
    const target = path.join(destination, name);
    const temporary = `${target}.${process.pid}.tmp`;
    fs.copyFileSync(path.join(source, name), temporary);
    fs.chmodSync(temporary, 0o600);
    fs.renameSync(temporary, target);
  }
  return destination;
}

module.exports = { REQUIRED_ORIGINAL_ASSETS, hasOriginalAssets, copyOriginalAssets };
