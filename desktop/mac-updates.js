const RELEASES_API = "https://api.github.com/repos/Ornn8/bside-olivia-community/releases?per_page=20";

function versionParts(value) {
  if (typeof value !== "string" || !/^v?\d+\.\d+\.\d+$/.test(value)) return null;
  return value.replace(/^v/, "").split(".").map(Number);
}

function newer(left, right) {
  const a = versionParts(left);
  const b = versionParts(right);
  if (!a || !b) return false;
  return a.some((part, index) => part !== b[index] && part > b[index] &&
    a.slice(0, index).every((earlier, earlierIndex) => earlier === b[earlierIndex]));
}

function availableMacRelease(releases, currentVersion, arch) {
  if (!Array.isArray(releases) || !["arm64", "x64"].includes(arch)) return null;
  const candidates = releases.filter((release) => !release.draft && !release.prerelease)
    .filter((release) => newer(release.tag_name, currentVersion))
    .sort((left, right) => newer(left.tag_name, right.tag_name) ? -1 : 1);
  for (const release of candidates) {
    const asset = release.assets?.find((entry) =>
      entry.name === `BSide Olivia-${release.tag_name.replace(/^v/, "")}-${arch}.dmg`);
    if (!asset || typeof asset.browser_download_url !== "string") continue;
    const url = new URL(asset.browser_download_url);
    if (url.protocol === "https:" && url.hostname === "github.com" &&
        url.pathname.startsWith("/Ornn8/bside-olivia-community/releases/download/")) {
      return { version: release.tag_name.replace(/^v/, ""), url: url.href };
    }
  }
  return null;
}

async function checkMacRelease(currentVersion, arch) {
  const response = await fetch(RELEASES_API, {
    headers: { Accept: "application/vnd.github+json", "User-Agent": "BSide-Olivia-Mac" },
    signal: AbortSignal.timeout(12000),
  });
  if (!response.ok) throw new Error("UPDATE_CHECK_FAILED");
  return availableMacRelease(await response.json(), currentVersion, arch);
}

module.exports = { availableMacRelease, checkMacRelease };
