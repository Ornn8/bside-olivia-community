# Personal messaging integration

## Scope and activation

Tencent's official Weixin authorization entry and a separately installed NapCat OneBot QQ account feed one `PersonalChatService` inside the existing local backend process. No OpenClaw agent personality, memory engine or separate backend data writer is added. The owner selected the official Weixin entry and confirmed receiving the fixed test reply on 2026-09-12. This is not ordinary Weixin-account automation and needs no additional character Weixin account.

This implementation is opt-in and has not been loaded into the running 1.1.20 installation. The connection probe has exited. Do not start a second local server against the live data root. Install these changes through the normal backend update before enabling them, or use an isolated synthetic data root for development.

Copy `config.example.json` to a private absolute path, fill the QQ account and owner, and omit any channel not used. Save it as `data/personal-chat/config.json` for normal launcher startup, or set `OLIVIA_PERSONAL_CHAT_CONFIG` to an explicit absolute path. For QQ use `credentials_file` pointing to a DPAPI-encrypted JSON object with a token field, or set `OLIVIA_PERSONAL_QQ_TOKEN` to the existing loopback OneBot server token. Never put the token in a command-line argument or checked-in config. Weixin reuses the DPAPI-encrypted credentials from the connection probe. Normal backend startup/shutdown owns both listeners. Remove both the saved config and config environment override, then restart to disable them; this does not delete chat history or application data.

The generic group bot is a separate repository and must not use these credentials or this data root.

## Processing and durability

- Exact channel/account/owner filtering happens before any model or store access. Only text is supported in this slice. Group events, foreign senders, self echoes and mixed media are rejected. `/连接测试` receives a fixed deduplicated transport reply and is excluded from normal generation and memory. Content-free status is written to `data/personal-chat-status.json`; LISTENING means the runner is active, while diagnostic roundtrips provide actual send evidence.
- Both personal channels share the configured model, ready persona asset, memory retrieval, original correspondence, relationship projection and world state. Only `future_im` delivery style changes; no letter salutation, signature or sticker envelope is requested. No routing-classifier or group-bot summarizer is added.
- Exchanges are persisted atomically in `state.json` under `personal_chats`; they never become native inbox letters. The transport cursor has its own `personal_chat_cursors` field, not public UI settings. Native state files from before this feature load with empty chat collections.
- The sequence is `GENERATING → GENERATED → SENDING → DELIVERED`. Persist before sending; only platform-confirmed sends become canonical `COMPLETED` exchanges. A lost acknowledgment remains `SENDING` and needs manual resolution, with no automatic resend or memory/world write.
- On replay, a known generation failure or abandoned generation can be tried again, capped at two total generation attempts. Exhaustion is an explicit failure and does not advance the Weixin batch cursor. This is a recovery allowance, not a background model retry loop. A saved `GENERATED` reply can resume without another model call.
- Confirmed exchanges use the same content-free Mem0 outbox and source IDs as letters, plus the existing world ledger, candidate extraction, daily-life extraction and relationship committer. Candidate completion is persisted so a crash between ledger commit and candidate extraction is recoverable. Local consumer recovery never resends a platform message.
- The two personal channels serialize their exchanges. A world-consumer failure preserves its delivered row for background recovery without stopping the channel. Failures are persisted and capped at three consumer attempts; exhausted rows stay visible in content-free diagnostics. Network reads have bounded backoff; uncertain sends are never retried by a transport. Sanitized channel failures are logged; no message bodies or credentials are logged by these adapters.

## Acceptance and remaining work

Tests exercise real local OneBot WebSockets, Weixin HTTP response handling, the native store load/save path, actual persona assembly and reply pipeline with a synthetic provider, shared-channel lifecycle, and acknowledged-only memory outbox delivery. These checks prove code wiring, not real-model chat quality or production rollout.

Remaining live gates: load this version in the installation; verify both owner accounts using the configured model; confirm a new fact is recalled across Weixin/QQ and reflected correctly in the existing memory/world views; measure generation and extraction usage. Media messages, configuration UI, and a user-facing resolution action for uncertain sends are not implemented in this slice. Do not describe the module as production-ready yet.

Upstreams: Tencent/openclaw-weixin `7c04adc3e95775efd661ab9fba0626d86d237713` (MIT, official protocol reference); NapCatQQ Shell `v4.18.19` (separate installation, no copied client implementation). Protocol behavior: https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md and https://napneko.github.io/guide/boot/Shell .

Run targeted verification from the repository root:

```powershell
python -m pytest tests/http/test_personal_chat_events.py tests/http/test_personal_chat_probe.py tests/http/test_personal_chat_wechat.py tests/http/test_personal_chat_qq.py tests/http/test_personal_chat_service.py tests/http/test_personal_chat_backend.py tests/memory/test_conversation_memory_outbox.py -q
```
