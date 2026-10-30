const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { after, before, test } = require("node:test");
const { once } = require("node:events");
const { startFrontendServer } = require("../frontend-server");

let frontend;
let backend;
let root;
let backendRequests = [];
const uncaught = [];
const recordUncaught = (error) => uncaught.push(error);

before(async () => {
  process.on("uncaughtException", recordUncaught);
  root = fs.mkdtempSync(path.join(os.tmpdir(), "olivia-stream-test-"));
  fs.writeFileSync(path.join(root, "index.html"), "<main>Original</main>");
  backend = http.createServer((request, response) => {
    backendRequests.push(request);
    response.writeHead(200, { "Content-Type": "video/mp4" });
    response.write(Buffer.alloc(64 * 1024));
    if (request.url === "/toy/media/broken.mp4") {
      // The backend process exits in the middle of a media response.
      setTimeout(() => response.socket.destroy(), 20);
      return;
    }
    // Otherwise keep streaming until the reader goes away.
    const timer = setInterval(() => response.write(Buffer.alloc(64 * 1024)), 10);
    response.on("close", () => clearInterval(timer));
  });
  await new Promise((resolve) => backend.listen(0, "127.0.0.1", resolve));
  const backendOrigin = `http://127.0.0.1:${backend.address().port}`;
  frontend = await startFrontendServer({
    backendUrl: (route) => backendOrigin + route,
    getFrontendRoot: () => root,
  });
});

after(async () => {
  process.off("uncaughtException", recordUncaught);
  await frontend.stop();
  backend.closeAllConnections();
  await new Promise((resolve) => backend.close(resolve));
  fs.rmSync(root, { recursive: true, force: true });
});

// An aborted HTTP message emits "error" before "close"; either means it ended.
function ended(stream) {
  return new Promise((resolve) => {
    stream.once("error", () => {});
    stream.once("close", resolve);
  });
}

function openStream(route) {
  return new Promise((resolve, reject) => {
    const request = http.get(frontend.origin + route, { headers: { Referer: frontend.entry } }, resolve);
    request.on("error", reject);
  });
}

test("a reader that disconnects mid-stream closes the backend request", async () => {
  backendRequests = [];
  const response = await openStream("/toy/media/song.mp4");
  assert.equal(response.statusCode, 200);
  await once(response, "data");
  response.destroy();
  const upstream = backendRequests[0];
  const closed = ended(upstream);
  const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error("UPSTREAM_STILL_OPEN")), 2000));
  await Promise.race([closed, timeout]);
  assert.deepEqual(uncaught, []);
});

test("a backend failure mid-stream ends the response without crashing", async () => {
  const response = await openStream("/toy/media/broken.mp4");
  assert.equal(response.statusCode, 200);
  response.resume();
  await ended(response);
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.deepEqual(uncaught, []);
});
