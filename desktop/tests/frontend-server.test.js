const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { after, before, test } = require("node:test");
const { once } = require("node:events");
const { startFrontendServer } = require("../frontend-server");

let frontend;
let backend;
let root;

before(async () => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), "olivia-proxy-test-"));
  fs.writeFileSync(path.join(root, "index.html"), "<main>Original</main>");
  backend = http.createServer((request, response) => {
    assert.equal(request.headers.origin, "https://olivia.local");
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ status: "READY", path: request.url }));
  });
  await new Promise((resolve) => backend.listen(0, "127.0.0.1", resolve));
  const backendOrigin = `http://127.0.0.1:${backend.address().port}`;
  frontend = await startFrontendServer({
    backendUrl: (route) => backendOrigin + route,
    getFrontendRoot: () => root,
  });
});

after(async () => {
  await new Promise((resolve) => frontend.server.close(resolve));
  await new Promise((resolve) => backend.close(resolve));
  fs.rmSync(root, { recursive: true, force: true });
});

test("original page can call its local APIs without a cookie", async () => {
  const page = await fetch(frontend.entry);
  assert.equal(page.status, 200);
  assert.match(page.headers.get("set-cookie"), /HttpOnly/);
  const status = await fetch(frontend.origin + "/toy/setup/status", {
    headers: { Referer: frontend.entry },
  });
  assert.equal(status.status, 200);
  assert.equal((await status.json()).path, "/toy/setup/status");
  const language = await fetch(frontend.origin + "/api/ui-language", {
    method: "POST", headers: { Referer: frontend.entry, "Content-Type": "application/json" },
    body: JSON.stringify({ language: "zh-cn" }),
  });
  assert.equal(language.status, 200);
  assert.equal((await language.json()).path, "/api/ui-language");
});

test("requests without the private entry referrer are rejected", async () => {
  for (const headers of [{}, { Referer: frontend.origin + "/other/index.html" }, { Origin: "https://outside.test", Referer: frontend.entry }]) {
    const response = await fetch(frontend.origin + "/toy/setup/status", { headers });
    assert.equal(response.status, 403);
  }
});

test("proxies the original WebSocket upgrade to the backend", async () => {
  const crlf = String.fromCharCode(13, 10);
  const websocketBackend = net.createServer((socket) => {
    let request = "";
    socket.on("data", (chunk) => {
      request += chunk.toString();
      if (request.includes(crlf + crlf)) {
        socket.write(["HTTP/1.1 101 Switching Protocols", "Upgrade: websocket", "Connection: Upgrade", "", ""].join(crlf));
      }
    });
  });
  await new Promise((resolve) => websocketBackend.listen(0, "127.0.0.1", resolve));
  const backendOrigin = `http://127.0.0.1:${websocketBackend.address().port}`;
  const proxy = await startFrontendServer({
    backendUrl: (route) => backendOrigin + route,
    backendHeaders: () => ({ "X-Olivia-Desktop-Token": "test-token" }),
    getFrontendRoot: () => root,
  });
  try {
    const endpoint = new URL(proxy.origin);
    const secret = new URL(proxy.entry).pathname.split("/")[1];
    const response = await new Promise((resolve, reject) => {
      const socket = net.connect(Number(endpoint.port), "127.0.0.1");
      let bytes = "";
      const timer = setTimeout(() => { socket.destroy(); reject(new Error("websocket upgrade timeout")); }, 2000);
      socket.on("connect", () => socket.write([
        "GET /ws HTTP/1.1", `Host: ${endpoint.host}`, "Connection: Upgrade", "Upgrade: websocket",
        `Origin: ${proxy.origin}`, `Cookie: olivia_ui=${secret}`, "Sec-WebSocket-Version: 13",
        "Sec-WebSocket-Key: dGVzdC1rZXk=", "", "",
      ].join(crlf)));
      socket.on("data", (chunk) => {
        bytes += chunk.toString();
        if (bytes.includes(crlf + crlf)) { clearTimeout(timer); socket.destroy(); resolve(bytes); }
      });
      socket.on("error", (error) => { clearTimeout(timer); reject(error); });
    });
    assert.match(response, /^HTTP\/1\.1 101 Switching Protocols/m);
  } finally {
    await new Promise((resolve) => proxy.server.close(resolve));
    await new Promise((resolve) => websocketBackend.close(resolve));
  }
});

test("shutdown closes an active WebSocket and its upstream connection", { timeout: 3000 }, async () => {
  const crlf = "\r\n";
  const sockets = new Set();
  const upstream = net.createServer((socket) => {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
    socket.once("data", () => socket.write([
      "HTTP/1.1 101 Switching Protocols", "Upgrade: websocket", "Connection: Upgrade", "", "",
    ].join(crlf)));
  });
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  const proxy = await startFrontendServer({
    backendUrl: (route) => `http://127.0.0.1:${upstream.address().port}${route}`,
    getFrontendRoot: () => root,
  });
  const endpoint = new URL(proxy.origin);
  const socket = net.connect(Number(endpoint.port), "127.0.0.1");
  try {
    await once(socket, "connect");
    const upgraded = once(socket, "data");
    socket.write([
      "GET /ws HTTP/1.1", `Host: ${endpoint.host}`, "Connection: Upgrade", "Upgrade: websocket",
      `Origin: ${proxy.origin}`, `Cookie: olivia_ui=${new URL(proxy.entry).pathname.split("/")[1]}`,
      "Sec-WebSocket-Version: 13", "Sec-WebSocket-Key: dGVzdC1rZXk=", "", "",
    ].join(crlf));
    assert.match((await upgraded)[0].toString(), /^HTTP\/1.1 101/);
    const closed = once(socket, "close");
    await proxy.stop();
    await closed;
    await new Promise((resolve) => upstream.close(resolve));
    assert.equal(sockets.size, 0);
  } finally {
    socket.destroy();
    for (const connection of sockets) connection.destroy();
    proxy.server.close();
    upstream.close();
  }
});
