# Native MIDI compatibility

The installed 0.0.9.627 frontend was inspected as inert archive contents. The
examined `assets/main-31595bd3.js` SHA-256 was
`844944d7811ae6a46752106122dd579b941712b3ff25c56986ed681cc05a4cb7`.
This is static contract evidence, not a claim of acceptance in the native host.

Observed consumers:

| Entry | Observed request/result |
| --- | --- |
| `fo` / `Hp` | `genObjectUploadUrl({filename,type})` returns `{url,key,headers}`; XHR PUT sends the file, and the resolved upload value is `key`. |
| `On` | `midi/generate({midiUrl,filename})` returns `jobId`. |
| `ds` | `listJobs({pageSize,cursor})` consumes `list`, `total`, `hasMore`, `nextCursor`; rows use `jobId`, `createdAt`, `filename`, `state`. |
| `tm` | `batchGetResult({jobIds:[...]})` consumes `results`, `generatedToday`, `dailyLimit`. |
| `jn` | `getGenerateResult({jobId})` consumes `state` and `info.videoUrls`. |
| `De` | Pending 1, running 2, finished 3, canceled 4, failed 5. |
| `MidiPreviewDialog` → `VideoPreviewDialog` → `BaseVideo` | The video.js source type is hardcoded to `video/mp4`; an MP3 URL must not be mislabeled as this type. |

The client therefore enhances the existing `.midi-upload-card` and
`.midi-upload-card-large` locations with an audio panel. It uses the same upload,
generate, list, result, cancel and delete routes, and consumes `info.audioUrl`
through a real HTML audio element with an explicit MP3 download link. No dummy
video or generated image is needed. `videoUrls` is empty because the server
produces audio. This enhancement also avoids the old frontend's fixed daily
quota and `.mid`-only file restriction. Original cards are hidden before the
capability request; the replacement appears only for `piano.enabled === true`.

Uploads are opaque, expiring, single-use local PUT tickets. Caller-provided
remote URLs and filesystem paths are never fetched. The local adapter checks
extension, byte limit and `MThd`, then uploads unchanged bytes for server-side
cleaning. Submission request IDs and payloads are persisted before submitting;
an ambiguous response retries that same payload. Completed jobs retain the
server metadata and durable memory source. Removing a list record hides it
without erasing that memory. Background polling continues after closing the
panel; reopening polls persisted task IDs.

Mock tests cover the real local HTTP chain and browser controls. The browser
evidence verifies visibility, input, upload and rendered audio/download controls;
it does not establish native-host codec playback, real-server generation or
human listening acceptance. Those remain joint integration checks when the
server enables the capability.

For 2.3.0 the panel requires valid server price ranges and renders them without a client price table. Its segment selector sends section; omitted section defaults to 开头. Uploads also enter the account-scoped collection with title, SHA-256, first upload time and source. The panel displays that collection and filters jobs/audio by account. See [piano-client-contract.md](piano-client-contract.md).

Original task results retain their complete-song metadata and use the capability duration bound (default 480 seconds; decoded audio allows five seconds of tolerance). MIDI bounds remain 120/125 seconds. The existing audio element and MP3 download routes accept these results. Synthetic OneBot tests cover an 8 MiB local MP3 path with success, rejection and uncertain acknowledgements; actual QQ transfer and listening still require developer acceptance.
