# Native QQ media observations

The client sends bounded media bytes to `POST /v1/media/observations`, using the
existing Olivia account and an idempotency key. The cloud chooses Gemini's native
`generateContent` endpoint and supplies the observation prompt. The selected chat
model still writes the final reply from the resulting attributed observations.
Ordinary image observation and chronological GIF frames use the existing Qwen
vision route. No provider credential is added to the client.
The request and successful response contracts are defined in
`native-media.schema.json` under `$defs.request` and `$defs.observation`.

Limits: at most four attachments in a QQ turn, four MiB per raw native attachment,
60 seconds for WAV/MP4, 20 pages for unencrypted PDF. Video is decoded and normalized
by the client's existing bundled FFmpeg, with its audio track retained. Animated
GIFs use at most four chronological samples; this is not complete frame analysis.
Video is normalized to two frames per second; fine or fast motion may be omitted.
Other file types are received as files but reported as unrecognized, never guessed.

NapCat `get_record` supplies decoded WAV base64. PDF download uses
`get_private_file_url`; video download uses `get_file`. Resource IDs from owner
events may not be paths or URLs. Only bounded base64 or approved QQ HTTPS CDN
URLs returned by the authenticated transport are accepted; local file paths,
redirects and arbitrary hosts are rejected. Recognition results remain quoted
media evidence, not user assertions or system instructions. Received bytes and
observations persist locally, and restored receipts retain the media resource IDs.
The conversation window and existing memory outbox retain these as separate,
attributed observations without replacing the original message text.

## Cloud preparation and release boundary

`install_native_media.py <staged-service-root>` applies the narrow Relay integration
only to the reviewed baseline hash. It does not restart, deploy, install dependencies
or change the production database. Install `requirements-native-media.txt` in the
cloud service environment before enabling PDF requests. Include
`nginx-native-media.conf` in the existing TLS server block: the current public
proxy has a strict route whitelist, so adding the ASGI endpoint alone is insufficient.
Validate the staged source and `nginx -t`, back up the old source/config, drain active
requests, then restart `qwen-relay.service` through systemd and reload nginx. Never
signal the Uvicorn master with SIGHUP. Rollback restores the backed-up source/config
and restarts the same service. No schema migration or client release is required
for cloud preparation; users require the matching client code for new QQ inputs.

The endpoint shares existing authentication, concurrency, idempotency and accounting.
Admission uses decoded duration/page count, not base64 byte length. Existing retail
rates and minimum charges are unchanged; money is not reserved. Confirmed native
usage includes thinking output and is settled once. An empty/truncated/malformed
answer is a failed observation even when HTTP is 200. Confirmed provider consumption
still settles under the existing rule; explicit pre-generation rejections cost zero,
while unknown usage stays unknown. Client retries/restarts never dispatch a possibly
paid observation again.

## Checks

Run `client/tests/http/test_qq_native_media.py` with the existing client tests.
Run `python -m unittest discover -s server -p test_native_media.py` in an environment
with httpx and pypdf. `test_native_media_billing.py` uses Django's isolated test database
and the staged live service sources; it must never point at a production database.

These checks use synthetic fixtures. A supplier call proves the adapter request and
response work for that fixture; it does not prove real QQ user/device acceptance.
