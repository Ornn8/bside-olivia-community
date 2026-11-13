const path = require("path");

// macOS treats these directories as single documents or programs: opening one
// with shell.openPath launches it instead of showing a folder.
const BUNDLE_SUFFIXES = new Set([
  ".app", ".appex", ".bundle", ".framework", ".kext", ".plugin", ".prefpane",
  ".qlgenerator", ".saver", ".service", ".systemextension", ".wdgt", ".xpc",
]);

function isBundlePath(target) {
  return String(target).split(path.sep).some((part) => BUNDLE_SUFFIXES.has(path.extname(part).toLowerCase()));
}

// Only a plain directory request that is not (inside) a bundle may be opened;
// everything else is revealed in Finder without being executed.
function openDecision(action, resolved, isDirectory) {
  return action === "openDirectory" && isDirectory && !isBundlePath(resolved) ? "open" : "reveal";
}

module.exports = { isBundlePath, openDecision };
