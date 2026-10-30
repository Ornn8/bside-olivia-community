const { contextBridge, ipcRenderer } = require("electron");

const bootstrap = ipcRenderer.sendSync("olivia:bootstrap");
if (bootstrap?.uid) {
  window.localStorage.setItem("uid", String(bootstrap.uid));
  window.localStorage.setItem("appMode", "lite");
}

contextBridge.exposeInMainWorld("cefViewQuery", ({ request, onSuccess, onFailure }) => {
  ipcRenderer.invoke("olivia:native-query", request)
    .then((result) => onSuccess(JSON.stringify(result)))
    .catch((error) => onFailure("NATIVE_QUERY_FAILED", error.message));
});

const nativeListeners = new Map();
function nativeEvent(name, payload) {
  for (const listener of nativeListeners.get(name) || []) listener(JSON.stringify(payload));
}
ipcRenderer.on("olivia:native-event", (_event, name, payload) => {
  nativeEvent(name, payload);
});
let player;
function mediaPlayer() {
  if (player) return player;
  player = document.createElement("video");
  player.preload = "auto";
  player.playsInline = true;
  player.style.display = "none";
  document.body.appendChild(player);
  player.addEventListener("timeupdate", () => nativeEvent("webPlayerControl", {
    event: "timeupdate", currentTime: player.currentTime,
  }));
  player.addEventListener("ended", () => nativeEvent("webPlayerControl", { event: "ended" }));
  return player;
}
function handlePlayerCommand(data) {
  if (!data || typeof data !== "object") return;
  const media = mediaPlayer();
  switch (data.cmd) {
    case "play": {
      const raw = data.song?.videoUrl;
      if (typeof raw !== "string" || raw.length > 8192) return;
      let url;
      try { url = new URL(raw, location.origin); }
      catch { return; }
      if (!(url.protocol === "https:" || (url.origin === location.origin &&
          (url.pathname.startsWith("/toy/") || url.pathname.startsWith("/desktop-downloads/") ||
           url.pathname.startsWith("/desktop-media/"))))) return;
      media.src = url.href;
      media.currentTime = 0;
      media.play().catch(() => nativeEvent("webPlayerControl", { event: "stop" }));
      break;
    }
    case "stop":
      media.pause();
      media.removeAttribute("src");
      media.load();
      break;
    case "setVolume":
      if (Number.isFinite(data.data?.mainVolume)) media.volume = Math.max(0, Math.min(1, data.data.mainVolume / 100));
      media.muted = data.data?.muted === true;
      break;
    case "timeupdate":
      if (Number.isFinite(data.position) && data.position >= 0 && media.seekable.length) media.currentTime = data.position;
      break;
  }
}
ipcRenderer.on("olivia:player-command", (_event, data) => handlePlayerCommand(data));
ipcRenderer.on("olivia:native-action", (_event, action, data) => {
  if (action === "collectionPlay") {
    handlePlayerCommand({ cmd: "play", song: data });
  } else if (action === "collectionStop") {
    handlePlayerCommand({ cmd: "stop" });
  } else if (action === "sendRenderVolumeControl") {
    handlePlayerCommand({ cmd: "setVolume", data });
  } else if (action === "updateRoute" && typeof data?.route === "string" && data.route.length <= 512) {
    // The original frontend uses hash routes. Keep the private entry pathname
    // so API referrers and the main-process origin check remain authenticated.
    const route = data.route.startsWith("/") ? data.route : "/" + data.route;
    window.history.pushState({}, "", "#" + route);
    window.dispatchEvent(new PopStateEvent("popstate"));
  } else {
    nativeEvent("nativeAction", { action, data });
  }
});
contextBridge.exposeInMainWorld("ToyPianistClient", {
  invoke: (action, data) => ipcRenderer.send("olivia:native-invoke", action, data),
  addEventListener: (name, callback) => {
    if (!nativeListeners.has(name)) nativeListeners.set(name, new Set());
    nativeListeners.get(name).add(callback);
  },
  removeEventListener: (name, callback) => nativeListeners.get(name)?.delete(callback),
});

contextBridge.exposeInMainWorld("olivia", {
  request: (method, route, body) => ipcRenderer.invoke("olivia:request", method, route, body),
  status: () => ipcRenderer.invoke("olivia:status"),
  setLoginItem: (enabled) => ipcRenderer.invoke("olivia:login-item", enabled),
  openRelease: () => ipcRenderer.invoke("olivia:release"),
  checkUpdates: () => ipcRenderer.invoke("olivia:check-updates"),
  connectAccount: () => ipcRenderer.invoke("olivia:connect-account"),
  exportBackup: () => ipcRenderer.invoke("olivia:backup"),
  importBackup: () => ipcRenderer.invoke("olivia:restore"),
  exportDiagnostics: () => ipcRenderer.invoke("olivia:diagnostics"),
  chooseAudio: () => ipcRenderer.invoke("olivia:choose-audio"),
  importSong: () => ipcRenderer.invoke("olivia:import-song"),
  chooseMidi: () => ipcRenderer.invoke("olivia:choose-midi"),
  savePiano: (jobId) => ipcRenderer.invoke("olivia:save-piano", jobId),
  onBackendStopped: (callback) => ipcRenderer.on("olivia:backend-stopped", () => callback()),
});
