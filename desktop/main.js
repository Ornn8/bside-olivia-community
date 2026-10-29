const { app, BrowserWindow, dialog, ipcMain, Menu, nativeImage, shell, Tray } = require("electron");
const { spawn } = require("child_process");
const { execFile } = require("child_process");
const { randomBytes } = require("crypto");
const fs = require("fs");
const path = require("path");
const readline = require("readline");
const { startFrontendServer } = require("./frontend-server");
const { checkMacRelease } = require("./mac-updates");
const { NativeFiles } = require("./native-files");
const trayIcon = require("./tray-icon");
const { hasOriginalAssets, copyOriginalAssets } = require("./original-assets");

const workspace = path.resolve(__dirname, "..");
const backendRoot = app.isPackaged ? path.join(process.resourcesPath, "backend") : path.join(workspace, "client");
const python = process.env.OLIVIA_DESKTOP_PYTHON || (app.isPackaged
  ? path.join(process.resourcesPath, "python", "bin", "python3.12")
  : path.join(workspace, "client", ".venv", "bin", "python"));
const dataRoot = process.env.OLIVIA_DESKTOP_DATA_ROOT || (app.isPackaged
  ? path.join(app.getPath("appData"), "BSide Olivia", "Data")
  : path.join(workspace, ".context", "olivia-desktop-dev", "Data"));
fs.mkdirSync(path.dirname(dataRoot), { recursive: true, mode: 0o700 });
app.setPath("userData", path.dirname(dataRoot));

let rendererUrl;
let originalAssetRoot = process.env.OLIVIA_ORIGINAL_ASSET_ROOT || (app.isPackaged
  ? path.join(path.dirname(dataRoot), "Original")
  : path.join(workspace, ".context", "windows-full", "source"));
const routes = {
  GET: [
    /^\/health$/, /^\/toy\/letter\/(?:list|detail|unread_count)$/, /^\/toy\/relationship$/,
    /^\/toy\/diary(?:\/entry|\/memoir)?$/, /^\/toy\/local-songs$/,
    /^\/toy\/companion\/(?:memory|status|private-world|daily-life)$/,
    /^\/toy\/companion\/private-world\/life$/,
    /^\/toy\/personal-chat\/setup\/status$/, /^\/toy\/settings\/(?:reply-routes|video-reply)$/,
    /^\/toy\/proactive\/status$/, /^\/toy\/setup\/status$/, /^\/toy\/media\/progress$/,
    /^\/toy\/world\/(?:wardrobe|gifts|pets)$/, /^\/toy\/sticker-packs$/,
    /^\/toy\/improve$/, /^\/toy\/midi\/(?:capabilities|listJobs)$/,
  ],
  POST: [
    /^\/toy\/letter\/(?:send|resend|route-preview|backup\/export|backup\/import)$/,
    /^\/toy\/relationship\/route$/, /^\/toy\/diary\/(?:settings|comment|delete|memoir\/start)$/,
    /^\/toy\/local-songs\/(?:from-letter|import|rename|delete|reveal)$/,
    /^\/toy\/companion\/memory\/(?:correct|delete|pause|resume)$/,
    /^\/toy\/personal-chat\/setup\/(?:channel-choice|wechat\/start|wechat\/verify|qq\/configure)$/,
    /^\/toy\/relay\/action$/, /^\/toy\/settings\/(?:reply-routes|video-reply)$/,
    /^\/toy\/proactive\/settings$/, /^\/toy\/setup\/(?:llm\/test|llm\/save|complete)$/,
    /^\/toy\/cover\/lyrics$/,
    /^\/toy\/generation\/action$/, /^\/toy\/world\/(?:wardrobe|gifts|pets)$/,
    /^\/toy\/improve\/(?:settings|forget)$/, /^\/toy\/sticker-packs\/open$/,
    /^\/toy\/companion\/memory\/(?:clear|retry)$/,
    /^\/toy\/companion\/private-world\/life$/,
    /^\/toy\/letter\/deletion\/(?:delete|restore|diary)$/,
    /^\/toy\/letter\/deletion\/list$/, /^\/toy\/genObjectUploadUrl$/,
    /^\/toy\/midi\/(?:generate|cancelGenerate|deleteJob)$/,
  ],
};

let backend;
let backendPort;
let mainWindow;
let tray;
let quitting = false;
let setupSession;
let backendToken;
let frontendRoot;
let frontendOrigin;
let frontendServer;
let stopFrontend;
let frontendOutput;
let originalUid;
let nativeSettings = {};
let nativeFiles;
let shutdownPromise;
let shutdownComplete = false;
let backendStopping = false;
let backendRecoveryPromise;
let backendRecoveryAttempts = 0;
let pendingSecondInstance = false;
const FRONTEND_PREPARE_TIMEOUT_MS = 90000;

async function ensureOriginalAssets() {
  if (hasOriginalAssets(originalAssetRoot)) return;
  if (process.env.OLIVIA_ORIGINAL_ASSET_ROOT || !app.isPackaged) {
    throw new Error("ORIGINAL_FRONTEND_ASSETS_REQUIRED");
  }
  const result = await dialog.showOpenDialog({
    title: "选择已取得授权的 Olivia Windows 客户端资源",
    message: "Mac 版需要原版界面资源才能保持 Windows 客户端界面。资源不会进入安装包或上传。",
    properties: ["openDirectory"],
  });
  if (result.canceled || !result.filePaths[0] || !hasOriginalAssets(result.filePaths[0])) {
    throw new Error("ORIGINAL_FRONTEND_ASSETS_REQUIRED");
  }
  originalAssetRoot = copyOriginalAssets(result.filePaths[0], originalAssetRoot);
}

async function checkForMacUpdates() {
  try {
    const release = await checkMacRelease(app.getVersion(), process.arch);
    const options = release ? {
      type: "info", title: "Olivia Mac 更新", message: `发现 Mac 版本 ${release.version}`,
      detail: "下载同架构 DMG 安装；应用数据保留在 Application Support。",
      buttons: ["稍后", "打开下载页"], defaultId: 1, cancelId: 0,
    } : {
      type: "info", title: "Olivia Mac 更新", message: "暂无公开的 Mac 更新",
      detail: `当前版本 ${app.getVersion()} (${process.arch})。`, buttons: ["关闭"],
    };
    const result = mainWindow ? await dialog.showMessageBox(mainWindow, options) : await dialog.showMessageBox(options);
    if (release && result.response === 1) await shell.openExternal(release.url);
  } catch {
    const options = { type: "warning", title: "Olivia Mac 更新",
      message: "暂时无法检查更新", detail: "请稍后重试。", buttons: ["关闭"] };
    if (mainWindow) await dialog.showMessageBox(mainWindow, options);
    else await dialog.showMessageBox(options);
  }
}

function createApplicationMenu() {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: "Olivia", submenu: [
      { role: "about" }, { label: "检查 Mac 更新...", click: checkForMacUpdates },
      { type: "separator" }, { role: "hide" }, { role: "hideOthers" }, { role: "unhide" },
      { type: "separator" }, { role: "quit" },
    ] },
    { role: "editMenu" }, { role: "viewMenu" }, { role: "windowMenu" },
  ]));
}

function prepareFrontend() {
  return new Promise((resolve, reject) => {
    // A fixed per-profile directory (guarded by the single-instance lock) is
    // wiped on every start, so a crash never leaves extracted assets behind.
    const output = path.join(path.dirname(dataRoot), "Frontend");
    fs.rmSync(output, { recursive: true, force: true });
    fs.mkdirSync(output, { recursive: true, mode: 0o700 });
    frontendOutput = output;
    let settled = false;
    const child = spawn(python, [path.join(backendRoot, "desktop_original_frontend.py"),
      "--source", originalAssetRoot, "--output", output, "--port", String(backendPort),
      "--proxy-origin", frontendOrigin], {
      cwd: backendRoot,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
      stdio: ["ignore", "pipe", "pipe"],
    });
    child.stdout.resume();
    child.stderr.resume();
    const cleanup = () => {
      try { child.kill("SIGTERM"); } catch { /* already exited */ }
      fs.rmSync(output, { recursive: true, force: true });
      if (frontendOutput === output) frontendOutput = undefined;
    };
    const fail = (error = new Error("ORIGINAL_FRONTEND_PREPARE_FAILED")) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      cleanup();
      reject(error);
    };
    const timer = setTimeout(() => fail(new Error("ORIGINAL_FRONTEND_PREPARE_TIMEOUT")), FRONTEND_PREPARE_TIMEOUT_MS);
    child.on("error", () => fail());
    child.on("exit", (code) => {
      if (settled) return;
      if (code !== 0 || !fs.existsSync(path.join(output, "frontend", "index.html"))) {
        fail();
        return;
      }
      settled = true;
      clearTimeout(timer);
      frontendRoot = path.join(output, "frontend");
      resolve();
    });
  });
}

async function readOriginalUid() {
  const response = await fetch(backendUrl("/toy/signIn"), {
    method: "POST", headers: backendHeaders({ Origin: "https://olivia.local", "Content-Type": "application/json" }), body: "{}",
  });
  const payload = await response.json();
  const uid = payload.data?.uid;
  if (!response.ok || (typeof uid !== "string" && !Number.isSafeInteger(uid))) throw new Error("ORIGINAL_LOGIN_UNAVAILABLE");
  originalUid = String(uid);
}

function backendUrl(route) {
  if (!backendPort) throw new Error("BACKEND_NOT_READY");
  const url = new URL(route, "http://127.0.0.1");
  if (!route.startsWith("/") || url.origin !== "http://127.0.0.1") throw new Error("ROUTE_INVALID");
  return `http://127.0.0.1:${backendPort}${url.pathname}${url.search}`;
}

function backendHeaders(headers = {}) {
  return { ...headers, "X-Olivia-Desktop-Token": backendToken };
}

async function request(method, route, body, internal = false, retried = false) {
  const url = new URL(route, "http://127.0.0.1");
  if (!routes[method]?.some((pattern) => pattern.test(url.pathname))) throw new Error("ROUTE_FORBIDDEN");
  if (!internal && url.pathname === "/toy/relay/action" && ![
    "account", "claim", "connect", "models", "select_model", "select_routes",
    "balance", "statement", "create_order", "order_status",
  ].includes(body?.action)) throw new Error("ACTION_FORBIDDEN");
  if (!internal && url.pathname === "/toy/generation/action" && ![
    "billing_quote", "music_settings_status", "music_settings_save",
  ].includes(body?.action)) throw new Error("ACTION_FORBIDDEN");
  const needsSession = url.pathname.startsWith("/toy/relay/") || url.pathname === "/toy/generation/action" ||
    (url.pathname.startsWith("/toy/setup/") && url.pathname !== "/toy/setup/status");
  if (needsSession && !setupSession) setupSession = (await request("GET", "/toy/setup/status")).session_token;
  const response = await fetch(backendUrl(route), {
    method,
    headers: backendHeaders({
      Origin: "https://olivia.local", Accept: "application/json",
      ...(method === "POST" ? { "Content-Type": "application/json", "X-Olivia-Companion-Action": "confirmed", "X-Olivia-Setup-Action": "confirmed" } : {}),
      ...(url.pathname.startsWith("/toy/personal-chat/setup/") ? { "X-Olivia-Companion-Action": "confirmed" } : {}),
      ...(needsSession ? { "X-Olivia-Setup-Session": setupSession } : {}),
    }),
    ...(method === "POST" ? { body: JSON.stringify(body || {}) } : {}),
    signal: AbortSignal.timeout(["/toy/letter/send", "/toy/letter/resend", "/toy/local-songs/import"].includes(url.pathname) ? 900000 : 60000),
  });
  const result = await response.json();
  if (needsSession && !retried && result.error_code === "LLM_SETUP_LOGIN_REQUIRED") {
    // The backend issues a new setup session whenever it restarts.
    setupSession = undefined;
    return request(method, route, body, internal, true);
  }
  if (!response.ok || result.code && result.code !== 0 || result.error || result.error_code) {
    throw new Error(result.error_code || result.error || result.data?.error_code || result.message || `HTTP_${response.status}`);
  }
  return result.data ?? result;
}

function startBackend() {
  return new Promise((resolve, reject) => {
    fs.mkdirSync(dataRoot, { recursive: true, mode: 0o700 });
    const nonce = randomBytes(24).toString("hex");
    backendToken = randomBytes(32).toString("hex");
    setupSession = undefined;
    backendStopping = false;
    const child = spawn(python, [path.join(backendRoot, "desktop_backend.py")], {
      cwd: backendRoot,
      env: { ...process.env, OLIVIA_LOCAL_DATA_ROOT: dataRoot, OLIVIA_PORT: "0", OLIVIA_DESKTOP_START_NONCE: nonce,
        OLIVIA_DESKTOP_REQUEST_TOKEN: backendToken,
        OLIVIA_DESKTOP_VERSION: app.getVersion(),
        ...(app.isPackaged ? { OLIVIA_DESKTOP_MODEL_CACHE: path.join(process.resourcesPath, "memory-model-cache") } : {}),
        OLIVIA_DESKTOP_PARENT_PIPE: "1", PYTHONUNBUFFERED: "1", PYTHONDONTWRITEBYTECODE: "1" },
      // stdin stays open for the backend's lifetime; when this process dies
      // (even by SIGKILL) the pipe closes and the backend stops itself.
      stdio: ["pipe", "pipe", "pipe"],
    });
    backend = child;
    let settled = false;
    let ready = false;
    const timeout = setTimeout(() => {
      if (!settled) {
        settled = true;
        backendStopping = true;
        child.kill("SIGTERM");
        reject(new Error("BACKEND_START_TIMEOUT"));
      }
    }, 90000);
    readline.createInterface({ input: child.stdout }).on("line", (line) => {
      try {
        const message = JSON.parse(line);
        if (message.schema_version !== "olivia-ready.v1" || message.kind !== "olivia-ready" ||
            !Number.isInteger(message.port) || message.nonce !== nonce) return;
        backendPort = message.port;
        ready = true;
        if (!settled) { settled = true; clearTimeout(timeout); resolve(); }
      } catch { /* Content-free backend logs are not protocol messages. */ }
    });
    child.stderr.resume();
    child.stdin.on("error", () => { /* The backend already exited. */ });
    child.on("error", (error) => {
      if (!settled) { settled = true; clearTimeout(timeout); reject(error); }
    });
    child.on("exit", (code) => {
      backendPort = undefined;
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        // 75: another backend still owns this data root (desktop_backend.py).
        reject(new Error(code === 75 ? "BACKEND_ALREADY_RUNNING" : "BACKEND_EXITED"));
      }
      if (ready && !backendStopping && !quitting) handleBackendStopped();
    });
  });
}

async function handleBackendStopped() {
  if (backendRecoveryPromise || quitting || !mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.webContents.send("olivia:backend-stopped");
  if (backendRecoveryAttempts >= 1) {
    const result = await dialog.showMessageBox(mainWindow, {
      type: "error", title: "Olivia 后端已停止",
      message: "本地服务意外退出，实时功能暂时不可用。",
      detail: "可以重试启动本地服务，或退出 Olivia。",
      buttons: ["重试", "退出"], defaultId: 0, cancelId: 1,
    });
    if (result.response === 0) {
      backendRecoveryAttempts = 0;
      handleBackendStopped();
    } else {
      quitting = true;
      app.quit();
    }
    return;
  }
  backendRecoveryAttempts += 1;
  backendRecoveryPromise = (async () => {
    let failed = false;
    try {
      await startBackend();
      await readOriginalUid();
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("olivia:backend-restarted");
        await mainWindow.webContents.reload();
      }
      backendRecoveryAttempts = 0;
      refreshTrayStatus().catch(() => {});
    } catch {
      failed = true;
    } finally {
      backendRecoveryPromise = undefined;
    }
    if (failed) await handleBackendStopped();
  })();
  await backendRecoveryPromise;
}

function createTray() {
  const icon = nativeImage.createFromBuffer(trayIcon);
  if (icon.isEmpty()) throw new Error("TRAY_ICON_INVALID");
  icon.setTemplateImage(true);
  tray = new Tray(icon);
  tray.setToolTip("Olivia");
  updateTrayMenu();
  tray.on("double-click", () => mainWindow?.show());
}

function updateTrayMenu(proactive, chat) {
  if (!tray) return;
  const channels = (chat?.selected_channels || []).filter((name) => chat?.configured?.[name]);
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "打开 Olivia", click: () => { mainWindow?.show(); app.focus({ steal: true }); } },
    { label: `主动联系：${proactive?.enabled ? "已开启" : "已关闭"}`, enabled: false },
    { label: `聊天连接：${channels.length ? channels.join("、") : "未开启"}`, enabled: false },
    { type: "separator" },
    { label: "退出 Olivia", click: () => { quitting = true; app.quit(); } },
  ]));
}

async function refreshTrayStatus() {
  if (!backendPort) return;
  const [proactive, chat] = await Promise.allSettled([
    request("GET", "/toy/proactive/status"),
    request("GET", "/toy/personal-chat/setup/status"),
  ]);
  updateTrayMenu(proactive.status === "fulfilled" ? proactive.value : null,
    chat.status === "fulfilled" ? chat.value : null);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200, height: 820, minWidth: 900, minHeight: 620,
    title: "Olivia", backgroundColor: "#17191d", titleBarStyle: "hiddenInset",
    trafficLightPosition: { x: 19, y: 19 },
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("https://")) shell.openExternal(url).catch(() => {});
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!url.startsWith(frontendOrigin + "/")) event.preventDefault();
  });
  mainWindow.on("close", (event) => {
    if (!quitting) { event.preventDefault(); mainWindow.hide(); refreshTrayStatus().catch(() => {}); }
  });
  mainWindow.loadURL(rendererUrl);
  if (pendingSecondInstance) {
    pendingSecondInstance = false;
    mainWindow.show();
    app.focus({ steal: true });
  }
}

function fromApp(event) {
  if (event.sender !== mainWindow?.webContents || !event.senderFrame?.url.startsWith(frontendOrigin + "/")) {
    throw new Error("WINDOW_FORBIDDEN");
  }
}

ipcMain.on("olivia:bootstrap", (event) => {
  try { fromApp(event); event.returnValue = { uid: originalUid }; }
  catch { event.returnValue = {}; }
});

ipcMain.handle("olivia:native-query", async (event, raw) => {
  fromApp(event);
  if (typeof raw !== "string" || raw.length > 1024 * 1024) throw new Error("NATIVE_QUERY_INVALID");
  const { action, data } = JSON.parse(raw);
  if (typeof action !== "string") throw new Error("NATIVE_QUERY_INVALID");
  switch (action) {
    case "getClientConfig":
      return {
        appConf: { toyApiUrl: frontendOrigin, toyWsUrl: frontendOrigin.replace(/^http:/, "ws:") + "/ws",
          globalGatewayUrl: "", otelAuthCredential: "", otelService: "", passportUrl: "",
          webAccountHost: "", region: "", signSalt: "", trackingUrl: "" },
        appInfo: { BundleId: "community.bside.olivia.mac", Channel: "local", ClientType: "pc",
          Company: "BSide Olivia Community", ConfigVersion: app.getVersion(), DisplayName: "Olivia",
          Language: "zh-CN", Product: "Olivia", ShowDebugPanel: false },
        apiHeaders: { "x-client_type": "pc_lite", "x-device_os": "macOS", "x-device_model": "Mac" },
        deviceFP: "", offlineMode: false,
      };
    case "getDeviceInfo": return { audioin: [], display: [] };
    case "getThirdpartyLoginInfo": return {};
    case "startClientApp":
      if (String(data?.uid) !== originalUid || data?.level !== "lite") throw new Error("NATIVE_LOGIN_INVALID");
      return { status: "READY" };
    case "sendWebPlayerControlCmd":
      if (!data || typeof data !== "object" || !["play", "stop", "setVolume", "timeupdate", "setPlayMode", "popupWidget"].includes(data.cmd)) {
        throw new Error("PLAYER_COMMAND_INVALID");
      }
      mainWindow.webContents.send("olivia:player-command", data);
      return { status: "READY" };
    case "getSettings": {
      try { nativeSettings = JSON.parse(fs.readFileSync(path.join(dataRoot, "native-settings.json"), "utf8")); }
      catch { nativeSettings = {}; }
      return nativeSettings;
    }
    case "updateSettings": {
      if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("NATIVE_SETTINGS_INVALID");
      nativeSettings = { ...nativeSettings, ...data };
      const target = path.join(dataRoot, "native-settings.json");
      const temporary = target + ".tmp";
      fs.writeFileSync(temporary, JSON.stringify(nativeSettings), { mode: 0o600 });
      fs.renameSync(temporary, target);
      return nativeSettings;
    }
    case "updateExtraHttpHeaders": return true;
    case "showFileDirectoryPicker": {
      const selected = await dialog.showOpenDialog(mainWindow, {
        defaultPath: typeof data?.initDir === "string" && path.isAbsolute(data.initDir) ? data.initDir : undefined,
        properties: ["openDirectory"],
      });
      return selected.canceled ? { path: "" } : nativeFiles.grantDirectory(selected.filePaths[0]);
    }
    case "saveBase64File": return nativeFiles.saveBase64File(data);
    case "startDownloadTasks": return nativeFiles.startDownloadTasks(data);
    case "getDownloadTasksProgress": return nativeFiles.getDownloadTasksProgress(data);
    case "cancelDownloadTasks": return nativeFiles.cancelDownloadTasks(data);
    case "getOfflineSongList": return nativeFiles.getOfflineSongList();
    case "checkLocalSongs": {
      if (!Array.isArray(data?.songs) || data.songs.length > 1000) throw new Error("SONG_CHECK_INVALID");
      const local = data.songs.some((song) => song?.oliviaLocal) ? await request("GET", "/toy/local-songs") : { songs: [] };
      const localIds = new Set(local.songs.map((song) => String(song.native_id)));
      return { songs: data.songs.map((song) => ({ songId: String(song?.id ?? ""),
        exist: song?.oliviaLocal ? localIds.has(String(song.id)) : nativeFiles.hasSong(song?.id) })) };
    }
    case "startSongDownload": {
      if (!Array.isArray(data?.songs)) throw new Error("SONG_DOWNLOAD_INVALID");
      const remote = data.songs.filter((song) => !song?.oliviaLocal);
      return remote.length ? nativeFiles.startSongDownload({ songs: remote }) : { status: "READY" };
    }
    case "getSongDownloadProgress": return nativeFiles.getSongDownloadProgress();
    case "cancelSongDownload": return nativeFiles.cancelSongDownload(data);
    case "startMediaDownload": return nativeFiles.startMediaDownload(data);
    case "stopMediaDownload": return nativeFiles.stopMediaDownload(data);
    case "checkLocalMedia": {
      const id = String(data?.mediaId || "").replace(/[^a-zA-Z0-9_-]/g, "_");
      return { exist: Boolean(nativeFiles.serveMedia(`${id}.mp4`) || nativeFiles.serveMedia(`${id}.wav`)) };
    }
    case "getMediaInfo": return nativeFiles.mediaInfo(data);
    case "startMediaEdit": return nativeFiles.startMediaEdit(data);
    case "stopMediaEdit": return nativeFiles.stopMediaEdit(data);
    case "saveMediaEdit": return nativeFiles.saveMediaEdit(data);
    default: throw new Error("NATIVE_ACTION_UNAVAILABLE");
  }
});

ipcMain.on("olivia:native-invoke", (event, action, data) => {
  fromApp(event);
  if (typeof action !== "string" || action.length > 80) return;
  if (action === "exitApp") { quitting = true; app.quit(); }
  else if (action === "restartApp") { quitting = true; app.relaunch(); app.quit(); }
  else if (action === "minimizeWindow") mainWindow.minimize();
  else if (action === "hideWindow") mainWindow.hide();
  else if (action === "openInBrowser") {
    try {
      const url = new URL(data?.url);
      if (url.protocol === "https:") shell.openExternal(url.href).catch(() => {});
    } catch { /* Ignore malformed external links. */ }
  } else if (action === "openDirectory" || action === "revealFileInExplorer") {
    const target = data?.path === "app://log" ? path.join(dataRoot, "diagnostics")
      : data?.path === "app://crash" ? dataRoot : data?.path;
    if (typeof target === "string" && path.isAbsolute(target) && fs.existsSync(target)) {
      const resolved = fs.realpathSync(target);
      if (resolved.startsWith(dataRoot + path.sep) || resolved === dataRoot || nativeFiles.granted.has(resolved) ||
          nativeFiles.granted.has(path.dirname(resolved))) {
        if (fs.statSync(resolved).isDirectory()) shell.openPath(resolved).catch(() => {});
        else shell.showItemInFolder(resolved);
      }
    }
  } else if (["collectionPlay", "collectionStop", "startChat", "startSolo", "startPlaysing",
    "startInstrumental", "sendRenderVolumeControl", "notifyLogout", "addCollectionItem",
    // updateRoute reports the renderer's route; echoing it creates a router loop.
    "eventTrack", "updateMailReddot", "toggleRenderLog", "sendRenderConsoleMessage"]
    .includes(action)) {
    mainWindow.webContents.send("olivia:native-action", action, data);
  }
});

ipcMain.handle("olivia:request", (event, method, route, body) => { fromApp(event); return request(method, route, body); });
  ipcMain.handle("olivia:status", (event) => { fromApp(event); return {
    ready: !!backendPort, version: app.getVersion(), platform: process.platform,
    managedNapcatSupported: process.platform === "win32",
    loginItem: app.getLoginItemSettings().openAtLogin,
  }; });
ipcMain.handle("olivia:login-item", (event, enabled) => {
  fromApp(event);
  if (typeof enabled !== "boolean") throw new Error("INVALID_SETTING");
  app.setLoginItemSettings({ openAtLogin: enabled, openAsHidden: true });
  return app.getLoginItemSettings().openAtLogin;
});
ipcMain.handle("olivia:release", (event) => { fromApp(event); return shell.openExternal("https://github.com/Ornn8/bside-olivia-community/releases/latest"); });
ipcMain.handle("olivia:check-updates", (event) => { fromApp(event); return checkForMacUpdates(); });
ipcMain.handle("olivia:connect-account", async (event) => {
  fromApp(event);
  const script = 'set answer to display dialog "Olivia Key" default answer "" with hidden answer buttons {"取消", "连接"} default button "连接"\nreturn text returned of answer';
  const key = await new Promise((resolve, reject) => {
    execFile("/usr/bin/osascript", ["-e", script], { timeout: 120000, maxBuffer: 1024 }, (error, stdout, stderr) => {
      // osascript exits 1 for every script error; only -128 is a user cancel.
      if (error && error.code === 1 && /\(-128\)/.test(stderr)) return resolve(null);
      if (error) return reject(new Error("KEY_PROMPT_FAILED"));
      resolve(stdout.trim());
    });
  });
  if (key === null) return false;
  if (!/^olivia-[A-Za-z0-9_-]{1,121}$/.test(key)) throw new Error("KEY_INVALID");
  await request("POST", "/toy/relay/action", { action: "import_key", key }, true);
  return true;
});
ipcMain.handle("olivia:backup", async (event) => {
  fromApp(event);
  const { canceled, filePath } = await dialog.showSaveDialog(mainWindow, { defaultPath: "olivia-letters.json", filters: [{ name: "JSON", extensions: ["json"] }] });
  if (canceled) return false;
  const result = await request("POST", "/toy/letter/backup/export", {});
  fs.writeFileSync(filePath, JSON.stringify(result.backup, null, 2), { mode: 0o600 });
  return true;
});
ipcMain.handle("olivia:restore", async (event) => {
  fromApp(event);
  const { canceled, filePaths } = await dialog.showOpenDialog(mainWindow, { properties: ["openFile"], filters: [{ name: "Olivia JSON", extensions: ["json"] }] });
  if (canceled) return null;
  if (fs.statSync(filePaths[0]).size > 32 * 1024 * 1024) throw new Error("BACKUP_TOO_LARGE");
  const backup = JSON.parse(fs.readFileSync(filePaths[0], "utf8"));
  return request("POST", "/toy/letter/backup/import", { backup });
});
ipcMain.handle("olivia:diagnostics", async (event) => {
  fromApp(event);
  const { canceled, filePath } = await dialog.showSaveDialog(mainWindow, { defaultPath: "olivia-diagnostic-bundle.zip", filters: [{ name: "ZIP", extensions: ["zip"] }] });
  if (canceled) return false;
  const response = await fetch(backendUrl("/toy/diagnostics/export"), { headers: backendHeaders({ Origin: "https://olivia.local" }), signal: AbortSignal.timeout(60000) });
  if (!response.ok || response.headers.get("content-type")?.includes("json")) throw new Error("DIAGNOSTIC_EXPORT_FAILED");
  const bytes = Buffer.from(await response.arrayBuffer());
  if (bytes.length > 32 * 1024 * 1024) throw new Error("DIAGNOSTIC_TOO_LARGE");
  fs.writeFileSync(filePath, bytes, { mode: 0o600 });
  return true;
});
ipcMain.handle("olivia:choose-audio", async (event) => {
  fromApp(event);
  const { canceled, filePaths } = await dialog.showOpenDialog(mainWindow, { properties: ["openFile"], filters: [{ name: "Audio", extensions: ["mp3", "wav", "m4a", "flac", "ogg"] }] });
  if (canceled) return null;
  if (fs.statSync(filePaths[0]).size > 100 * 1024 * 1024) throw new Error("AUDIO_TOO_LARGE");
  const response = await fetch(backendUrl("/toy/cover/upload"), {
    method: "POST", headers: backendHeaders({ Origin: "https://olivia.local", "X-Olivia-Companion-Action": "confirmed" }),
    body: fs.readFileSync(filePaths[0]), signal: AbortSignal.timeout(180000),
  });
  const result = await response.json();
  if (!response.ok || result.code !== 0) throw new Error(result.data?.error_code || result.message || "UPLOAD_FAILED");
  return { name: path.basename(filePaths[0]), ...result.data };
});
ipcMain.handle("olivia:import-song", async (event) => {
  fromApp(event);
  const { canceled, filePaths } = await dialog.showOpenDialog(mainWindow, { properties: ["openFile"], filters: [
    { name: "Audio and video", extensions: ["mp3", "wav", "m4a", "flac", "ogg", "mp4", "mov", "mkv", "webm"] },
  ] });
  if (canceled) return null;
  return request("POST", "/toy/local-songs/import", { path: filePaths[0] });
});
ipcMain.handle("olivia:choose-midi", async (event) => {
  fromApp(event);
  const { canceled, filePaths } = await dialog.showOpenDialog(mainWindow, { properties: ["openFile"], filters: [{ name: "MIDI", extensions: ["mid", "midi"] }] });
  if (canceled) return null;
  const filePath = filePaths[0];
  const name = path.basename(filePath);
  const caps = await request("GET", "/toy/midi/capabilities");
  if (!caps.enabled || !Number.isSafeInteger(caps.max_midi_bytes)) throw new Error("PIANO_UNAVAILABLE");
  const stat = fs.statSync(filePath);
  if (stat.size > caps.max_midi_bytes || stat.size < 14 || !/\.midi?$/i.test(name)) throw new Error("MIDI_INVALID");
  const bytes = fs.readFileSync(filePath);
  if (bytes.subarray(0, 4).toString("ascii") !== "MThd") throw new Error("MIDI_INVALID");
  const ticket = await request("POST", "/toy/genObjectUploadUrl", { filename: name });
  if (!/^[a-f0-9]{32}$/.test(ticket.key)) throw new Error("MIDI_TICKET_INVALID");
  const response = await fetch(backendUrl(`/toy/midi/upload/${ticket.key}`), {
    method: "PUT", headers: backendHeaders({ Origin: "https://olivia.local", "Content-Type": "audio/midi" }),
    body: bytes, signal: AbortSignal.timeout(180000),
  });
  if (!response.ok) throw new Error("MIDI_UPLOAD_FAILED");
  return { key: ticket.key, name, title: name.replace(/\.midi?$/i, "").slice(0, 60) };
});
ipcMain.handle("olivia:save-piano", async (event, jobId) => {
  fromApp(event);
  if (typeof jobId !== "string" || !/^[a-f0-9]{32}$/.test(jobId)) throw new Error("MIDI_JOB_INVALID");
  const { canceled, filePath } = await dialog.showSaveDialog(mainWindow, { defaultPath: "piano.mp3", filters: [{ name: "MP3", extensions: ["mp3"] }] });
  if (canceled) return false;
  const response = await fetch(backendUrl(`/toy/midi/audio/${jobId}.mp3`), { headers: backendHeaders({ Origin: "https://olivia.local" }), signal: AbortSignal.timeout(60000) });
  if (!response.ok) throw new Error("MIDI_DOWNLOAD_FAILED");
  const bytes = Buffer.from(await response.arrayBuffer());
  if (bytes.length > 50 * 1024 * 1024) throw new Error("MIDI_DOWNLOAD_TOO_LARGE");
  fs.writeFileSync(filePath, bytes, { mode: 0o600 });
  return true;
});

const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
      app.focus({ steal: true });
    } else {
      pendingSecondInstance = true;
    }
  });
  app.whenReady().then(async () => {
    createApplicationMenu();
    createTray();
    try {
      await startBackend();
      ({ server: frontendServer, stop: stopFrontend, origin: frontendOrigin, entry: rendererUrl } = await startFrontendServer({
        backendUrl, backendHeaders: () => backendHeaders(), getFrontendRoot: () => frontendRoot,
        getDownloadRoot: () => nativeFiles.songRoot,
        getMediaRoot: () => nativeFiles.mediaRoot,
      }));
      nativeFiles = new NativeFiles({ dataRoot, backendOrigin: frontendOrigin, backendUrl,
        backendHeaders: (headers) => backendHeaders(headers) });
      nativeFiles.onMediaProgress = (payload) => mainWindow?.webContents.send("olivia:native-event", "mediaDownloadProgress", payload);
      nativeFiles.onMediaEditProgress = (payload) => mainWindow?.webContents.send("olivia:native-event", "mediaEditProgress", payload);
      await ensureOriginalAssets();
      await prepareFrontend();
      await readOriginalUid();
      refreshTrayStatus().catch(() => {});
      createWindow();
    } catch (error) {
      dialog.showErrorBox("Olivia 无法启动", error.message);
      app.quit();
      return;
    }
    app.on("activate", () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); else mainWindow.show(); });
  });
}

function stopBackendProcess() {
  if (!backend || backend.exitCode !== null) return Promise.resolve();
  backendStopping = true;
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(() => {
      backend.kill("SIGKILL");
      finish();
    }, 5000);
    backend.once("exit", finish);
    backend.kill("SIGTERM");
  });
}

async function shutdown() {
  nativeFiles?.shutdown();
  if (stopFrontend) await stopFrontend();
  await stopBackendProcess();
  if (frontendOutput) fs.rmSync(frontendOutput, { recursive: true, force: true });
}

app.on("before-quit", (event) => {
  if (shutdownComplete) return;
  event.preventDefault();
  if (shutdownPromise) return;
  quitting = true;
  shutdownPromise = shutdown().finally(() => {
    shutdownComplete = true;
    app.quit();
  });
});
app.on("window-all-closed", () => {});
