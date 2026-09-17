# Personal messaging integration

## Scope and activation

Tencent's official Weixin authorization entry and a separately installed NapCat OneBot QQ account feed one `PersonalChatService` inside the existing local backend process. No OpenClaw agent personality, memory engine or separate backend data writer is added. The owner selected the official Weixin entry and confirmed receiving the fixed test reply on 2026-09-12. This is not ordinary Weixin-account automation and needs no additional character Weixin account.

This implementation is an opt-in preview. It has been deployed to the development installation, but this does not establish new-user or phone-side acceptance. Do not start a second local server against the live data root. Install these changes through the normal backend update before enabling them, or use an isolated synthetic data root for development. See `docs/personal-chat-release-acceptance.md` for the release boundary.

The normal user flow begins only after the existing contact-invitation system records an evidenced QQ/Weixin choice. The existing settings page then shows the selected channel under **QQ / 微信聊天**:

- **Weixin:** choose **生成二维码**, scan the locally rendered QR code with Weixin, confirm on the phone, and enter the phone verification code only if Tencent returns `need_verifycode`. Successful credentials are protected with the existing Windows DPAPI helper and saved under `data/personal-chat/`; restart Olivia once to load the new listener.
- **QQ (experimental):** install and start NapCat separately, enable its loopback OneBot WebSocket, then enter the character QQ, owner QQ, local WebSocket URL and OneBot token. The setup action verifies `get_login_info` before saving. The token is DPAPI-protected and is not written to `config.json`; restart Olivia once to load the listener.

Advanced/manual configuration remains supported for diagnostics. Copy `config.example.json` to a private absolute path, fill the QQ account and owner, and omit any channel not used. Save it as `data/personal-chat/config.json` for normal launcher startup, or set `OLIVIA_PERSONAL_CHAT_CONFIG` to an explicit absolute path. For QQ use `credentials_file` pointing to a DPAPI-encrypted JSON object with a token field, or set `OLIVIA_PERSONAL_QQ_TOKEN` to the existing loopback OneBot server token. Never put the token in a command-line argument or checked-in config. Weixin reuses the DPAPI-encrypted credentials produced by setup. Normal backend startup/shutdown owns both listeners. Remove both the saved config and config environment override, then restart to disable them; this does not delete chat history or application data.

Configuration alone does not grant contact access. At least three relationship dimensions must be high, backed by the existing relationship evidence, before an invitation can be offered; the selected channel follows the user's evidenced answer. The setup APIs reject channels that have not been accepted. Existing development bindings are local-only and must never be shipped.

Qualified contact invitations are due immediately and take priority on the next five-minute proactive check. They bypass ordinary-topic cooldown and discretionary planning, use text, and retain the proactive opt-in, current availability, unread, in-flight and delivery-quota gates. Failed invitation attempts retry no sooner than five minutes, at most three times per rolling day, persisted across restarts. Completed invitations and the user's channel/refusal choices prevent repeats.

The generic group bot is a separate repository and must not use these credentials or this data root.

## Processing and durability

- Exact channel/account/owner filtering happens before any model or store access. Text and platform-provided WeChat voice transcriptions can be inputs; no new ASR is included. Group events, foreign senders and self echoes are rejected. `/连接测试` receives a fixed deduplicated transport reply and is excluded from normal generation and memory. Content-free status is written to `data/personal-chat-status.json`; LISTENING means the runner is active, while diagnostic roundtrips provide actual send evidence.
- Both personal channels share the configured model, ready persona asset, memory retrieval, original correspondence, relationship projection and world state. Only `future_im` delivery style changes. One structured response controls text, preferences, appointments and optional stickers; no separate routing-classifier or group-bot summarizer is added. WeChat sends text and occasional unlocked PNG stickers; QQ can retain configured native voice replies. WeChat voice bars and calls are not supported.
- Exchanges are persisted atomically in `state.json` under `personal_chats`; they never become native inbox letters. The transport cursor has its own `personal_chat_cursors` field, not public UI settings. Native state files from before this feature load with empty chat collections.
- The sequence is `GENERATING → GENERATED → SENDING → DELIVERED`. Persist before sending; only platform-confirmed sends become canonical `COMPLETED` exchanges. A lost acknowledgment remains `SENDING` and needs manual resolution, with no automatic resend or memory/world write.
- A known generation failure can be retried, capped at two total generation attempts. Exhaustion remains a durable failure but no longer stops the listener or pins the WeChat cursor; subsequent messages can proceed. A saved `GENERATED` reply can resume without another model call. Unknown `SENDING` delivery is never blindly retried.
- Confirmed exchanges use the same content-free Mem0 outbox and source IDs as letters, plus the existing world ledger, candidate extraction, daily-life extraction and relationship committer. Candidate completion is persisted so a crash between ledger commit and candidate extraction is recoverable. Local consumer recovery never resends a platform message.
- The two personal channels serialize their exchanges. A world-consumer failure preserves its delivered row for background recovery without stopping the channel. Failures are persisted and capped at three consumer attempts; exhausted rows stay visible in content-free diagnostics. Network reads have bounded backoff; uncertain sends are never retried by a transport. Sanitized channel failures are logged; no message bodies or credentials are logged by these adapters.

## Setup security boundary

The settings setup API requires the same explicit local companion-action header used by other mutating settings actions. It exposes only content-free setup/listener state plus a locally rendered SVG QR data URL. Tencent's QR polling identifier, verification code, Bot token, account binding and filesystem credential paths are not returned to the settings UI. QR content is encoded locally rather than loaded as a remote image. QQ endpoints accept loopback WebSocket URLs only and verify the selected character account before persistence.

## Acceptance and remaining work

Tests exercise local setup gating, QR rendering/redaction, QQ plaintext-token exclusion, real local OneBot WebSockets, Weixin HTTP response handling, the native store load/save path, actual persona assembly and reply pipeline with a synthetic provider, shared-channel lifecycle, and acknowledged-only memory outbox delivery. These checks prove code wiring, not real-model chat quality or production rollout.

Remaining live gates: accept the new settings binding flow on the packaged Windows client, confirm a new fact is recalled across WeChat/QQ and reflected correctly in the existing memory/world views, and accept new proactive/sticker deliveries on the phone. A user-facing resolution action for uncertain sends is still not implemented. After restart, a new inbound message is needed to establish a live proactive sending target. The application must be running; expired appointments do not become an offline backlog. Do not describe the module as production-ready yet.

Upstreams: Tencent/openclaw-weixin `7c04adc3e95775efd661ab9fba0626d86d237713` (MIT, official protocol reference); NapCatQQ Shell `v4.18.19` (separate installation, no copied client implementation). Protocol behavior: https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md and https://napneko.github.io/guide/boot/Shell .

Run targeted verification from the repository root:

```powershell
python -m pytest tests/http/test_personal_chat_setup.py tests/http/test_personal_chat_events.py tests/http/test_personal_chat_probe.py tests/http/test_personal_chat_wechat.py tests/http/test_personal_chat_qq.py tests/http/test_personal_chat_service.py tests/http/test_personal_chat_backend.py tests/memory/test_conversation_memory_outbox.py tests/installer/test_patch_companion_settings.py -q
```
