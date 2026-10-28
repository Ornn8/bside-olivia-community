const { randomBytes } = require("crypto");
const fs = require("fs");
const http = require("http");
const net = require("net");
const path = require("path");
const { Readable } = require("stream");

const staticTypes = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
  ".webp": "image/webp", ".png": "image/png", ".mp4": "video/mp4",
  ".ttf": "font/ttf", ".woff": "font/woff", ".woff2": "font/woff2",
};

async function sendFile(request, response, filename, cookie) {
  let stat;
  try { stat = await fs.promises.stat(filename); }
  catch { response.writeHead(404).end(); return; }
  if (!stat.isFile()) { response.writeHead(404).end(); return; }
  const headers = {
    "Content-Type": staticTypes[path.extname(filename)] || "application/octet-stream",
    "Accept-Ranges": "bytes", "Cache-Control": "no-store",
    ...(cookie ? { "Set-Cookie": cookie } : {}),
  };
  let start = 0;
  let end = stat.size - 1;
  const range = /^bytes=(\d+)-(\d*)$/.exec(request.headers.range || "");
  if (range) {
    start = Number(range[1]);
    end = range[2] ? Math.min(Number(range[2]), end) : end;
    if (start >= stat.size || end < start) { response.writeHead(416).end(); return; }
    headers["Content-Range"] = `bytes ${start}-${end}/${stat.size}`;
  }
  headers["Content-Length"] = end - start + 1;
  response.writeHead(range ? 206 : 200, headers);
  if (request.method === "HEAD") { response.end(); return; }
  fs.createReadStream(filename, { start, end }).on("error", () => response.destroy()).pipe(response);
}

async function readBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 128 * 1024 * 1024) throw new Error("FRONTEND_REQUEST_TOO_LARGE");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function rewriteBackendUrls(value, key, backendOrigin, frontendOrigin) {
  if (Array.isArray(value)) return value.map((item) => rewriteBackendUrls(item, key, backendOrigin, frontendOrigin));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([name, item]) =>
      [name, rewriteBackendUrls(item, name, backendOrigin, frontendOrigin)]));
  }
  if (typeof value === "string" && key && /(?:url|uri|file|path)$/i.test(key) &&
      (value === backendOrigin || value.startsWith(backendOrigin + "/"))) {
    return frontendOrigin + value.slice(backendOrigin.length);
  }
  return value;
}

function startFrontendServer({ backendUrl, backendHeaders = () => ({}), getFrontendRoot, getDownloadRoot, getMediaRoot }) {
  const secret = randomBytes(32).toString("hex");
  const sockets = new Set();
  let stopping;
  let origin;
  const server = http.createServer(async (request, response) => {
    try {
      const url = new URL(request.url, origin);
      if (url.origin !== origin || request.headers.host !== `127.0.0.1:${server.address().port}`) {
        response.writeHead(404).end(); return;
      }
      const entry = `/${secret}/index.html`;
      const initial = request.method === "GET" && url.pathname === entry;
      const cookie = `olivia_ui=${secret}`;
      const hasCookie = (request.headers.cookie || "").split(";").some((item) => item.trim() === cookie);
      const entryUrl = origin + entry;
      const fromFrontend = request.headers.referer === entryUrl || request.headers.referer?.startsWith(entryUrl + "#");
      if (!initial && !hasCookie && !fromFrontend) {
        response.writeHead(403).end(); return;
      }
      if (request.headers.origin && request.headers.origin !== origin) {
        response.writeHead(403).end(); return;
      }
      const downloaded = /^\/desktop-downloads\/(\d{1,20})\.mp4$/.exec(url.pathname);
      if (downloaded && request.method === "GET") {
        await sendFile(request, response, path.join(getDownloadRoot(), downloaded[1] + ".mp4"));
        return;
      }
      const media = /^\/desktop-media\/([A-Za-z0-9_-]{1,256}\.(?:mp4|wav))$/.exec(url.pathname);
      if (media && request.method === "GET") {
        await sendFile(request, response, path.join(getMediaRoot(), media[1]));
        return;
      }
      if (url.pathname.startsWith("/toy/") || url.pathname === "/health" || url.pathname === "/api/ui-language") {
        const headers = new Headers();
        for (const [name, value] of Object.entries(request.headers)) {
          if (!["host", "cookie", "origin", "referer", "connection", "content-length", "accept-encoding"].includes(name)) {
            headers.set(name, Array.isArray(value) ? value.join(", ") : value);
          }
        }
        headers.set("Origin", "https://olivia.local");
        for (const [name, value] of Object.entries(backendHeaders())) headers.set(name, value);
        const body = ["GET", "HEAD"].includes(request.method) ? undefined : await readBody(request);
        const upstream = await fetch(backendUrl(url.pathname + url.search), {
          method: request.method, headers, body, signal: AbortSignal.timeout(900000),
        });
        const contentType = upstream.headers.get("content-type") || "application/octet-stream";
        if (contentType.includes("json")) {
          let payload = await upstream.text();
          try {
            payload = JSON.stringify(rewriteBackendUrls(JSON.parse(payload), "", new URL(backendUrl("/" )).origin, origin));
          } catch { /* Preserve non-JSON error bodies verbatim. */ }
          response.writeHead(upstream.status, { "Content-Type": contentType, "Cache-Control": "no-store" });
          response.end(payload);
          return;
        }
        const outgoing = { "Content-Type": contentType, "Cache-Control": "no-store" };
        for (const name of ["content-range", "accept-ranges", "content-length"]) {
          if (upstream.headers.has(name)) outgoing[name] = upstream.headers.get(name);
        }
        response.writeHead(upstream.status, outgoing);
        if (upstream.body) Readable.fromWeb(upstream.body).pipe(response);
        else response.end();
        return;
      }
      const prefix = `/${secret}/`;
      if (!url.pathname.startsWith(prefix)) { response.writeHead(404).end(); return; }
      const frontendRoot = getFrontendRoot();
      if (!frontendRoot) { response.writeHead(503).end(); return; }
      let relative;
      try { relative = decodeURIComponent(url.pathname.slice(prefix.length)); }
      catch { response.writeHead(404).end(); return; }
      if (!relative || relative === "index.html" || !relative.startsWith("assets/")) relative = "index.html";
      const filename = path.resolve(frontendRoot, relative);
      if (!filename.startsWith(frontendRoot + path.sep)) { response.writeHead(404).end(); return; }
      await sendFile(request, response, filename,
        initial ? `${cookie}; HttpOnly; SameSite=Strict; Path=/` : undefined);
    } catch (error) {
      if (!response.headersSent) response.writeHead(502).end();
      else response.destroy();
    }
  });
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
  });
  server.on("clientError", (_error, socket) => socket.destroy());
  server.on("upgrade", (request, socket, head) => {
    // The original client opens one long-lived WebSocket for push events.  The
    // HTTP proxy above cannot handle an upgrade, so bridge the raw handshake
    // to the authenticated Python server instead of merely rewriting the URL.
    try {
      const url = new URL(request.url, origin);
      const expectedHost = `127.0.0.1:${server.address().port}`;
      const cookie = `olivia_ui=${secret}`;
      const hasCookie = (request.headers.cookie || "").split(";").some((item) => item.trim() === cookie);
      if (url.origin !== origin || request.headers.host !== expectedHost || url.pathname !== "/ws" ||
          (request.headers.origin && request.headers.origin !== origin) || !hasCookie ||
          String(request.headers.upgrade || "").toLowerCase() !== "websocket") {
        socket.destroy();
        return;
      }
      const upstreamUrl = new URL(backendUrl(url.pathname + url.search));
      if (upstreamUrl.protocol !== "http:" || upstreamUrl.hostname !== "127.0.0.1" ||
          !Number.isInteger(upstreamUrl.port ? Number(upstreamUrl.port) : 80)) {
        socket.destroy();
        return;
      }
      const upstream = net.connect(Number(upstreamUrl.port || 80), upstreamUrl.hostname);
      upstream.once("connect", () => {
        const lines = [`GET ${upstreamUrl.pathname}${upstreamUrl.search} HTTP/1.1`];
        for (const [name, value] of Object.entries(request.headers)) {
          if (["host", "connection", "upgrade", "content-length"].includes(name)) continue;
          if (Array.isArray(value)) lines.push(`${name}: ${value.join(", ")}`);
          else if (value !== undefined) lines.push(`${name}: ${value}`);
        }
        lines.push(`Host: ${upstreamUrl.host}`, "Connection: Upgrade", "Upgrade: websocket");
        for (const [name, value] of Object.entries(backendHeaders())) lines.push(`${name}: ${value}`);
        upstream.write(`${lines.join("\r\n")}\r\n\r\n`);
        if (head?.length) upstream.write(head);
        socket.pipe(upstream).pipe(socket);
      });
      const close = () => {
        if (!socket.destroyed) socket.destroy();
        if (!upstream.destroyed) upstream.destroy();
      };
      upstream.once("error", close);
      socket.once("error", close);
      socket.once("close", () => upstream.destroy());
      upstream.once("close", () => { if (!socket.destroyed) socket.end(); });
    } catch {
      socket.destroy();
    }
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      origin = `http://127.0.0.1:${server.address().port}`;
      const stop = () => {
        if (!stopping) {
          stopping = new Promise((done) => {
            server.close(done);
            // server.close() waits forever for upgraded WebSocket connections.
            for (const socket of sockets) socket.destroy();
          });
        }
        return stopping;
      };
      resolve({ server, stop, origin, entry: `${origin}/${secret}/index.html` });
    });
  });
}

module.exports = { startFrontendServer };
