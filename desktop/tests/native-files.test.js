const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { after, test } = require("node:test");
const { NativeFiles } = require("../native-files");

const root = fs.mkdtempSync(path.join(os.tmpdir(), "olivia-native-files-"));
after(() => fs.rmSync(root, { recursive: true, force: true }));

test("original-client folder result and base64 save stay in selected directory", async () => {
  const selected = path.join(root, "selected");
  fs.mkdirSync(selected);
  const files = new NativeFiles({ dataRoot: root, backendOrigin: "http://127.0.0.1:1", backendUrl: (route) => route });
  assert.deepEqual(files.grantDirectory(selected), { path: fs.realpathSync(selected) });
  const saved = await files.saveBase64File({ base64: "data:image/png;base64,YWJj", fullFilePath: path.join(selected, "mail.png") });
  assert.equal(fs.readFileSync(saved.path, "utf8"), "abc");
  await assert.rejects(files.saveBase64File({ base64: "YWJj", fullFilePath: path.join(root, "outside.png") }), /FILE_DESTINATION_NOT_SELECTED/);
  await assert.rejects(files.saveBase64File({ base64: "YWJj", fullFilePath: saved.path }), /EEXIST/);
});

test("download tasks report completion and save local media without exposing the frontend cookie", async () => {
  const selected = path.join(root, "downloads");
  fs.mkdirSync(selected);
  const server = http.createServer((request, response) => {
    assert.equal(request.headers.origin, "https://olivia.local");
    response.writeHead(200, { "Content-Type": "audio/wav", "Content-Length": "4" });
    response.end("RIFF");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const origin = `http://127.0.0.1:${server.address().port}`;
    const files = new NativeFiles({ dataRoot: root, backendOrigin: origin, backendUrl: (route) => origin + route });
    files.grantDirectory(selected);
    const { data } = files.startDownloadTasks({ sourceUrls: [origin + "/toy/media/test.wav"], destPath: selected });
    const id = data[0];
    await files.tasks.get(id).promise;
    assert.deepEqual(files.getDownloadTasksProgress({ taskIds: [id] }).data.map((task) => task.state), [3]);
    assert.equal(fs.readFileSync(path.join(selected, "test.wav"), "utf8"), "RIFF");
    assert.throws(() => files.startDownloadTasks({ sourceUrls: ["http://example.com/test.wav"], destPath: selected }), /DOWNLOAD_URL_INVALID/);
    assert.throws(() => files.startDownloadTasks({ sourceUrls: [origin + "/health"], destPath: selected }), /DOWNLOAD_URL_INVALID/);
  } finally { server.close(); }
});

test("song downloads restore from the Mac data directory after restart", async () => {
  const dataRoot = path.join(root, "song-data");
  fs.mkdirSync(dataRoot);
  const server = http.createServer((_request, response) => {
    response.writeHead(200, { "Content-Type": "video/mp4", "Content-Length": "4" });
    response.end("MP4!");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const origin = `http://127.0.0.1:${server.address().port}`;
    const create = () => new NativeFiles({ dataRoot, backendOrigin: origin, backendUrl: (route) => origin + route });
    const files = create();
    assert.deepEqual(files.startSongDownload({ songs: [{ id: "123", name: "Test", videoUrl: origin + "/toy/media/test.mp4" }] }), { status: "READY" });
    await files.songTasks.get("123").promise;
    assert.equal(files.getSongDownloadProgress().songs[0].state, 3);
    assert.equal(fs.readFileSync(path.join(files.songRoot, "123.mp4"), "utf8"), "MP4!");
    const restored = create();
    assert.equal(restored.hasSong("123"), true);
    assert.equal(JSON.parse(restored.getOfflineSongList().data).songs[0].videoUrl,
      origin + "/desktop-downloads/123.mp4");
  } finally { server.close(); }
});

test("media failures remove the temporary download", async () => {
  const dataRoot = path.join(root, "media-failure");
  fs.mkdirSync(dataRoot);
  const files = new NativeFiles({ dataRoot, backendOrigin: "http://127.0.0.1:1", backendUrl: () => "" });
  const progress = [];
  files.onMediaProgress = (payload) => progress.push(payload);
  files.fetchMedia = async (_url, target) => {
    fs.writeFileSync(target, "partial");
    throw new Error("MEDIA_DOWNLOAD_FAILED");
  };
  files.startMediaDownload({ mediaId: "clip", performanceUrl: "https://media.example/clip.mp4", videoByTodView: [{ url: "https://media.example/clip.mp4" }] });
  await files.mediaTasks.get("clip").promise;
  assert.equal(fs.existsSync(path.join(files.mediaRoot, "clip.mp4.download")), false);
  assert.equal(files.mediaTasks.get("clip").error, "MEDIA_DOWNLOAD_FAILED");
  assert.equal(progress.at(-1).error, "MEDIA_DOWNLOAD_FAILED");
});

test("media edits report start and completion events", async () => {
  const dataRoot = path.join(root, "media-edit-progress");
  fs.mkdirSync(dataRoot);
  const files = new NativeFiles({ dataRoot, backendOrigin: "http://127.0.0.1:1", backendUrl: () => "" });
  const source = path.join(files.mediaRoot, "clip.mp4");
  fs.writeFileSync(source, "synthetic");
  files.runFfmpeg = async () => ({ stdout: "", stderr: "" });
  const progress = [];
  files.onMediaEditProgress = (payload) => progress.push(payload);
  const result = await files.startMediaEdit({ mediaId: "clip", editId: "a".repeat(32), videoByTodView: [{url: "x"}] });
  assert.equal(result.originFilePath, path.join(files.mediaRoot, `${"a".repeat(32)}.mp4`));
  assert.deepEqual(progress.map((item) => [item.id, item.progress, item.complete]), [
    ["a".repeat(32), 0, false], ["a".repeat(32), 100, true],
  ]);
});

test("media downloads reject redirects before requesting the target", async () => {
  let targetRequests = 0;
  const server = http.createServer((request, response) => {
    if (request.url === "/toy/go") {
      response.writeHead(302, { Location: "/toy/secret" });
      response.end();
    } else {
      targetRequests += 1;
      response.writeHead(200, { "Content-Length": "4" });
      response.end("leak");
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const origin = `http://127.0.0.1:${server.address().port}`;
    const files = new NativeFiles({ dataRoot: path.join(root, "media-redirect"), backendOrigin: origin, backendUrl: (route) => origin + route });
    const task = { controller: new AbortController() };
    await assert.rejects(files.fetchMedia(origin + "/toy/go", path.join(files.mediaRoot, "redirect.download"), task));
    assert.equal(targetRequests, 0);
  } finally { server.close(); }
});

test("downloads reject private HTTPS endpoints", async () => {
  const dataRoot = path.join(root, "media-private-endpoint");
  fs.mkdirSync(dataRoot);
  const files = new NativeFiles({ dataRoot, backendOrigin: "http://127.0.0.1:1", backendUrl: () => "" });
  assert.throws(() => files.validSource("https://127.0.0.1/private.mp4"), /DOWNLOAD_URL_INVALID/);
  assert.throws(() => files.validSource("https://[::1]/private.mp4"), /DOWNLOAD_URL_INVALID/);
  assert.throws(() => files.validSource("https://[::ffff:7f00:1]/private.mp4"), /DOWNLOAD_URL_INVALID/);
  await assert.rejects(
    files.fetchMedia("https://192.168.1.1/private.mp4", path.join(files.mediaRoot, "private.download"), {
      controller: new AbortController(),
    }),
    /DOWNLOAD_URL_INVALID/,
  );
});
