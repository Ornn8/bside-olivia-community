const { randomBytes } = require("crypto");
const dns = require("dns").promises;
const fs = require("fs");
const net = require("net");
const path = require("path");
const { execFile } = require("child_process");

const MAX_FILE_BYTES = 256 * 1024 * 1024;

function privateIpv4(value) {
  const octets = value.split(".").map(Number);
  if (octets.length !== 4 || octets.some((octet) => !Number.isInteger(octet) || octet < 0 || octet > 255)) return false;
  const [a, b] = octets;
  return a === 0 || a === 10 || a === 127 || a >= 224 ||
    (a === 100 && b >= 64 && b <= 127) || (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) || (a === 192 && (b === 0 || b === 2 || b === 168)) ||
    (a === 198 && b >= 18 && b <= 19);
}

function privateIpv6(value) {
  const normalized = value.replace(/^\[/, "").replace(/\]$/, "").toLowerCase();
  if (net.isIPv6(normalized) !== true) return false;
  if (normalized === "::" || normalized === "::1" || normalized.startsWith("fc") || normalized.startsWith("fd") ||
      normalized.startsWith("fe8") || normalized.startsWith("fe9") || normalized.startsWith("fea") ||
      normalized.startsWith("feb") || normalized.startsWith("ff")) return true;
  const mapped = normalized.match(/::ffff:(\d+(?:\.\d+){3})$/);
  if (mapped) return privateIpv4(mapped[1]);
  const mappedHex = normalized.match(/^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/);
  if (!mappedHex) return false;
  const high = Number.parseInt(mappedHex[1], 16);
  const low = Number.parseInt(mappedHex[2], 16);
  return privateIpv4([
    high >> 8, high & 0xff, low >> 8, low & 0xff,
  ].join("."));
}

function privateHost(hostname) {
  const host = String(hostname || "").replace(/^\[/, "").replace(/\]$/, "").replace(/\.$/, "").toLowerCase();
  if (!host || host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local") || host.endsWith(".internal")) return true;
  if (net.isIPv4(host)) return privateIpv4(host);
  if (net.isIPv6(host)) return privateIpv6(host);
  return false;
}

async function assertPublicHttpsSource(url) {
  if (url.protocol !== "https:") return;
  if (privateHost(url.hostname)) throw new Error("DOWNLOAD_URL_INVALID");
  if (net.isIP(url.hostname)) return;
  let addresses;
  try {
    addresses = await dns.lookup(url.hostname, { all: true, verbatim: true });
  } catch {
    return; // fetch will report an ordinary DNS failure without leaking details.
  }
  if (addresses.some(({ address }) => privateHost(address))) throw new Error("DOWNLOAD_URL_INVALID");
}

function safeName(value) {
  const name = path.basename(value || "download").replace(/[^\p{L}\p{N} ._()-]/gu, "_").replace(/^\.+/, "").slice(0, 120);
  return name || "download";
}

class NativeFiles {
  constructor({ dataRoot, backendOrigin, backendUrl, backendHeaders = (headers) => headers }) {
    this.dataRoot = dataRoot;
    this.backendOrigin = backendOrigin;
    this.backendUrl = backendUrl;
    this.backendHeaders = backendHeaders;
    this.granted = new Set();
    this.tasks = new Map();
    this.mediaTasks = new Map();
    this.mediaProcesses = new Map();
    this.mediaRoot = path.join(dataRoot, "media-downloads");
    fs.mkdirSync(this.mediaRoot, { recursive: true, mode: 0o700 });
    this.songTasks = new Map();
    this.songRoot = path.join(dataRoot, "downloaded-songs");
    this.songCatalog = path.join(this.songRoot, "catalog.json");
    this.songs = new Map();
    fs.mkdirSync(this.songRoot, { recursive: true, mode: 0o700 });
    try {
      const catalog = JSON.parse(fs.readFileSync(this.songCatalog, "utf8"));
      if (catalog.schema_version === 1 && Array.isArray(catalog.songs)) {
        for (const song of catalog.songs) {
          if (/^\d{1,20}$/.test(song.id) && song.file === `${song.id}.mp4` &&
              fs.existsSync(path.join(this.songRoot, song.file))) this.songs.set(song.id, song);
        }
      }
    } catch { /* No song catalog yet. */ }
  }

  grantDirectory(directory) {
    if (typeof directory !== "string" || !path.isAbsolute(directory)) throw new Error("DIRECTORY_INVALID");
    const real = fs.realpathSync(directory);
    if (!fs.statSync(real).isDirectory()) throw new Error("DIRECTORY_INVALID");
    this.granted.add(real);
    return { path: real };
  }

  destination(filename, song = false) {
    if (typeof filename !== "string" || !path.isAbsolute(filename)) throw new Error("FILE_DESTINATION_INVALID");
    const directory = fs.realpathSync(path.dirname(filename));
    if (song ? directory !== fs.realpathSync(this.songRoot) : !this.granted.has(directory)) {
      throw new Error("FILE_DESTINATION_NOT_SELECTED");
    }
    return path.join(directory, safeName(path.basename(filename)));
  }

  async saveBase64File({ base64, fullFilePath }) {
    if (typeof base64 !== "string" || base64.length > MAX_FILE_BYTES * 1.4) throw new Error("BASE64_FILE_INVALID");
    const encoded = base64.replace(/^data:[\w+./-]+;base64,/, "");
    if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)) throw new Error("BASE64_FILE_INVALID");
    const bytes = Buffer.from(encoded, "base64");
    if (bytes.length > MAX_FILE_BYTES) throw new Error("FILE_TOO_LARGE");
    const target = this.destination(fullFilePath);
    fs.writeFileSync(target, bytes, { flag: "wx", mode: 0o600 });
    return { path: target };
  }

  validSource(raw) {
    if (typeof raw !== "string" || raw.length > 8192) throw new Error("DOWNLOAD_URL_INVALID");
    const url = new URL(raw);
    if (url.username || url.password || url.hash) throw new Error("DOWNLOAD_URL_INVALID");
    if (url.protocol === "http:" && url.origin === this.backendOrigin && url.pathname.startsWith("/toy/")) return url;
    if (url.protocol === "https:" && !privateHost(url.hostname)) return url;
    throw new Error("DOWNLOAD_URL_INVALID");
  }

  async _download(url, destination, task) {
    const temporary = destination + "." + task.id + ".part";
    try {
      await assertPublicHttpsSource(url);
      const local = url.origin === this.backendOrigin;
      const source = local ? this.backendUrl(url.pathname + url.search) : url;
      const response = await fetch(source, {
        redirect: "error", headers: local ? this.backendHeaders({ Origin: "https://olivia.local" }) : {},
        signal: AbortSignal.any([task.controller.signal, AbortSignal.timeout(180000)]),
      });
      if (!response.ok || !response.body) throw new Error("DOWNLOAD_FAILED");
      const expected = Number(response.headers.get("content-length") || 0);
      if (expected > MAX_FILE_BYTES) throw new Error("FILE_TOO_LARGE");
      task.totalBytes = expected;
      const handle = fs.openSync(temporary, "wx", 0o600);
      try {
        for await (const chunk of response.body) {
          task.downloadedBytes += chunk.length;
          if (task.downloadedBytes > MAX_FILE_BYTES) throw new Error("FILE_TOO_LARGE");
          fs.writeSync(handle, chunk);
        }
        fs.fsyncSync(handle);
      } finally { fs.closeSync(handle); }
      if (task.controller.signal.aborted) throw new Error("DOWNLOAD_CANCELLED");
      fs.linkSync(temporary, destination);
      fs.unlinkSync(temporary);
      task.state = 3;
      return destination;
    } catch (error) {
      task.state = task.controller.signal.aborted ? 5 : 4;
      task.error = error.message;
      throw error;
    } finally { try { fs.unlinkSync(temporary); } catch { /* No partial file. */ } }
  }

  startDownloadTasks({ sourceUrls, destPath }) {
    if (!Array.isArray(sourceUrls) || !sourceUrls.length || sourceUrls.length > 20) throw new Error("DOWNLOAD_TASKS_INVALID");
    const directory = this.grantSelected(destPath);
    const urls = sourceUrls.map((source) => this.validSource(source));
    const result = {};
    for (const [index, url] of urls.entries()) {
      const id = randomBytes(16).toString("hex");
      const task = { id, state: 1, totalBytes: 0, downloadedBytes: 0, controller: new AbortController() };
      this.tasks.set(id, task);
      result[index] = id;
      const filename = safeName(decodeURIComponent(url.pathname.split("/").pop() || "download"));
      const target = this.destination(path.join(directory, filename));
      task.state = 2;
      task.promise = this._download(url, target, task).catch(() => {});
    }
    return { data: result };
  }

  grantSelected(directory) {
    if (typeof directory !== "string" || !path.isAbsolute(directory)) throw new Error("DIRECTORY_INVALID");
    const real = fs.realpathSync(directory);
    if (!this.granted.has(real)) throw new Error("DIRECTORY_NOT_SELECTED");
    return real;
  }

  getDownloadTasksProgress({ taskIds }) {
    if (!Array.isArray(taskIds) || taskIds.length > 20) throw new Error("DOWNLOAD_TASKS_INVALID");
    return { data: taskIds.map((id) => {
      const task = this.tasks.get(id);
      return task ? { taskId: id, state: task.state, totalBytes: task.totalBytes,
        downloadedBytes: task.downloadedBytes, downloadSpeed: 0 } : { taskId: id, state: 4, totalBytes: 0, downloadedBytes: 0 };
    }) };
  }

  cancelDownloadTasks({ taskIds }) {
    if (!Array.isArray(taskIds) || taskIds.length > 20) throw new Error("DOWNLOAD_TASKS_INVALID");
    for (const id of taskIds) this.tasks.get(id)?.controller.abort();
    return true;
  }

  saveSongCatalog() {
    const temporary = this.songCatalog + "." + randomBytes(8).toString("hex") + ".tmp";
    fs.writeFileSync(temporary, JSON.stringify({ schema_version: 1, songs: [...this.songs.values()] }), { mode: 0o600 });
    fs.renameSync(temporary, this.songCatalog);
  }

  startSongDownload({ songs }) {
    if (!Array.isArray(songs) || !songs.length || songs.length > 100) throw new Error("SONG_DOWNLOAD_INVALID");
    for (const song of songs) {
      const id = String(song?.id ?? "");
      if (!/^\d{1,20}$/.test(id)) throw new Error("SONG_DOWNLOAD_INVALID");
      if (this.songs.has(id) || this.songTasks.has(id)) continue;
      const source = song.videoUrl || song.videoByTodView?.[0]?.url;
      const url = this.validSource(source);
      const target = this.destination(path.join(this.songRoot, `${id}.mp4`), true);
      const task = { id: randomBytes(16).toString("hex"), state: 2, totalBytes: 0,
        downloadedBytes: 0, controller: new AbortController() };
      this.songTasks.set(id, task);
      task.promise = this._download(url, target, task).then(() => {
        task.state = 3;
        this.songs.set(id, {
          id, file: `${id}.mp4`, name: String(song.name || "Song").slice(0, 120),
          nameKey: String(song.nameKey || "").slice(0, 120),
          styleType: String(song.styleType || "").slice(0, 80),
          performanceType: String(song.performanceType || "").slice(0, 40),
          duration: Number.isFinite(song.duration) ? song.duration : 0,
        });
        this.saveSongCatalog();
      }).catch(() => { task.state = task.controller.signal.aborted ? 5 : 4; });
    }
    return { status: "READY" };
  }

  getSongDownloadProgress() {
    return { songs: [...this.songTasks].map(([songId, task]) => ({ songId, state: task.state,
      progress: task.totalBytes ? Math.min(100, Math.round(task.downloadedBytes / task.totalBytes * 100)) : 0,
      totalBytes: task.totalBytes, downloadedBytes: task.downloadedBytes, downloadSpeed: 0 })) };
  }

  cancelSongDownload({ songIdList }) {
    if (!Array.isArray(songIdList) || songIdList.length > 100) throw new Error("SONG_DOWNLOAD_INVALID");
    for (const id of songIdList) this.songTasks.get(String(id))?.controller.abort();
    return true;
  }

  ffmpeg() {
    const root = process.resourcesPath && path.join(process.resourcesPath, "python", "lib", "python3.12", "site-packages", "imageio_ffmpeg", "binaries");
    if (root && fs.existsSync(root)) {
      const candidate = fs.readdirSync(root).find((file) => file.startsWith("ffmpeg-"));
      if (candidate) return path.join(root, candidate);
    }
    return process.env.OLIVIA_FFMPEG_EXE || "ffmpeg";
  }

  runFfmpeg(args, timeout = 180000, processKey = null) {
    return new Promise((resolve, reject) => {
      const child = execFile(this.ffmpeg(), args, { timeout, maxBuffer: 4 * 1024 * 1024 }, (error, stdout, stderr) => {
        if (processKey) this.mediaProcesses.delete(processKey);
        if (error) reject(Object.assign(new Error("MEDIA_PROCESS_FAILED"), { stderr }));
        else resolve({ stdout, stderr });
      });
      if (processKey) this.mediaProcesses.set(processKey, child);
    });
  }

  async fetchMedia(url, target, task) {
    const parsed = this.validSource(url);
    await assertPublicHttpsSource(parsed);
    const source = parsed.origin === this.backendOrigin ? this.backendUrl(parsed.pathname + parsed.search) : parsed;
    const response = await fetch(source, { redirect: "error", headers: parsed.origin === this.backendOrigin ? this.backendHeaders({ Origin: "https://olivia.local" }) : {},
      signal: AbortSignal.any([task.controller.signal, AbortSignal.timeout(900000)]) });
    if (!response.ok || !response.body) throw new Error("MEDIA_DOWNLOAD_FAILED");
    const expected = Number(response.headers.get("content-length") || 0);
    if (expected > MAX_FILE_BYTES) throw new Error("FILE_TOO_LARGE");
    let received = 0;
    const handle = fs.openSync(target, "wx", 0o600);
    try {
      for await (const chunk of response.body) {
        received += chunk.length;
        if (task.mediaId && expected > 0) {
          task.progress = Math.min(60, Math.floor(received / expected * 60));
          this.reportMediaProgress(task);
        }
        if (received > MAX_FILE_BYTES) throw new Error("FILE_TOO_LARGE");
        fs.writeSync(handle, chunk);
      }
      fs.fsyncSync(handle);
    }
    finally { fs.closeSync(handle); }
    return target;
  }

  reportMediaProgress(task, force = false) {
    if (!task.mediaId || !this.onMediaProgress) return;
    const now = Date.now();
    if (!force && task.progress === task.lastReportedProgress && now - task.lastReportedAt < 100) return;
    task.lastReportedProgress = task.progress;
    task.lastReportedAt = now;
    this.onMediaProgress({ id: task.mediaId, progress: task.progress, complete: task.complete, error: task.error });
  }

  startMediaDownload({ mediaId, performanceUrl, videoByTodView }) {
    if (typeof mediaId !== "string" || mediaId.length > 256) throw new Error("MEDIA_ID_INVALID");
    const url = performanceUrl || videoByTodView?.[0]?.url;
    const task = { id: mediaId, mediaId, controller: new AbortController(), progress: 0, complete: false, error: null,
      lastReportedProgress: -1, lastReportedAt: 0 };
    this.mediaTasks.set(mediaId, task);
    const extension = videoByTodView?.length ? ".mp4" : ".wav";
    const target = path.join(this.mediaRoot, mediaId.replace(/[^a-zA-Z0-9_-]/g, "_") + extension);
    const raw = target + ".download";
    task.promise = (async () => {
      await this.fetchMedia(url, raw, task);
      task.progress = 60;
      this.reportMediaProgress(task, true);
      await this.runFfmpeg(["-v", "error", "-nostdin", "-y", "-i", raw, "-c", "copy", target], 180000, `media:${mediaId}`);
      fs.unlinkSync(raw);
      task.progress = 100;
      task.complete = true;
    })().catch((error) => { task.error = error.message; }).finally(() => {
      try { fs.unlinkSync(raw); } catch { /* Failed downloads are retryable. */ }
      this.reportMediaProgress(task, true);
    });
    return { status: "READY" };
  }

  stopMediaDownload({ mediaId }) {
    this.mediaTasks.get(mediaId)?.controller.abort();
    this.mediaProcesses.get(`media:${mediaId}`)?.kill("SIGTERM");
    return { status: "READY" };
  }

  mediaInfo({ mediaId, videoByTodView }) {
    const extension = videoByTodView?.length ? ".mp4" : ".wav";
    const safe = String(mediaId || "").replace(/[^a-zA-Z0-9_-]/g, "_");
    const file = path.join(this.mediaRoot, safe + extension);
    if (!fs.existsSync(file)) throw new Error("MEDIA_NOT_DOWNLOADED");
    return this.runFfmpeg(["-v", "info", "-i", file, "-f", "null", "-"]).then(({ stderr }) => {
      const match = /Duration: (\d+):(\d+):(\d+(?:\.\d+)?)/.exec(stderr);
      const duration = match ? Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]) : 0;
      const dimensions = /Video:.*?(\d{2,5})x(\d{2,5})/.exec(stderr);
      return { mediaId, file: this.backendOrigin + "/desktop-media/" + path.basename(file), duration,
        width: dimensions ? Number(dimensions[1]) : 0, height: dimensions ? Number(dimensions[2]) : 0,
        thumbs: [], samples: [] };
    });
  }

  async startMediaEdit({ mediaId, editId, x = 0, y = 0, w = 0, h = 0, start = 0, end = 0, videoByTodView }) {
    const extension = videoByTodView?.length ? ".mp4" : ".wav";
    const safeMedia = String(mediaId || "").replace(/[^a-zA-Z0-9_-]/g, "_");
    const source = path.join(this.mediaRoot, safeMedia + extension);
    if (!fs.existsSync(source) || !/^[a-f0-9]{32}$/.test(String(editId))) throw new Error("MEDIA_EDIT_INVALID");
    const output = path.join(this.mediaRoot, String(editId) + extension);
    const args = ["-v", "error", "-nostdin", "-y", "-ss", String(Math.max(0, Number(start) || 0))];
    if (Number(end) > Number(start)) args.push("-t", String(Number(end) - Number(start)));
    args.push("-i", source);
    if (extension === ".mp4" && Number(w) > 0 && Number(h) > 0) args.push("-vf", `crop=${Math.floor(w)}:${Math.floor(h)}:${Math.max(0, Math.floor(x))}:${Math.max(0, Math.floor(y))}`);
    if (extension === ".mp4") args.push("-c:v", "libx264", "-c:a", "aac");
    else args.push("-vn", "-c:a", "pcm_s16le");
    args.push(output);
    const report = (progress, complete = false, error = null) => {
      this.onMediaEditProgress?.({ id: editId, progress, complete, error });
    };
    report(0);
    try {
      await this.runFfmpeg(args, 180000, `edit:${editId}`);
    } catch (error) {
      report(0, false, error.message);
      throw error;
    }
    report(100, true);
    return { file: this.backendOrigin + "/desktop-media/" + path.basename(output), originFilePath: output };
  }

  stopMediaEdit({ editId }) {
    if (typeof editId === "string") this.mediaProcesses.get(`edit:${editId}`)?.kill("SIGTERM");
    return { status: "READY" };
  }

  async saveMediaEdit({ originFilePath, targetFilePath }) {
    if (typeof originFilePath !== "string" || typeof targetFilePath !== "string" || !path.isAbsolute(originFilePath)) throw new Error("MEDIA_SAVE_INVALID");
    const source = fs.realpathSync(originFilePath);
    if (!source.startsWith(fs.realpathSync(this.mediaRoot) + path.sep)) throw new Error("MEDIA_SAVE_INVALID");
    const target = this.destination(targetFilePath);
    fs.copyFileSync(source, target, fs.constants.COPYFILE_EXCL);
    return { path: target };
  }

  serveMedia(name) {
    if (!/^[A-Za-z0-9_-]{1,256}\.(?:mp4|wav)$/.test(name)) return null;
    const target = path.join(this.mediaRoot, name);
    return fs.existsSync(target) ? target : null;
  }

  hasSong(id) { return this.songs.has(String(id)); }

  getOfflineSongList() {
    const songs = [...this.songs.values()].map((song) => ({
      ...song, videoUrl: `${this.backendOrigin}/desktop-downloads/${song.file}`,
      videoByTodView: [{ url: `${this.backendOrigin}/desktop-downloads/${song.file}`, tod: "TOD12", view: "NI" }],
    }));
    return { data: JSON.stringify({ songs, musicStyles: [...new Set(songs.map((song) => song.styleType))]
      .filter(Boolean).map((type) => ({ type, displayName: type })), performanceModes: [] }) };
  }

  shutdown() {
    for (const task of this.tasks.values()) task.controller.abort();
    for (const task of this.songTasks.values()) task.controller.abort();
    for (const task of this.mediaTasks.values()) task.controller.abort();
    for (const child of this.mediaProcesses.values()) child.kill("SIGTERM");
  }
}

module.exports = { NativeFiles };
