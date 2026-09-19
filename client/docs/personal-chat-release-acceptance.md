# Personal chat preview and relationship fixes

## Shipping scope

Meaningful exchanges and shared experiences contribute bounded, evidence-backed relationship growth. Rewritten replies and reclassified identical user evidence do not award daily growth twice. A high relationship does not itself grant a romantic stage or intimacy permission.

Personal QQ and Weixin integration remain opt-in and disabled until a contact invitation exists and the user selects the corresponding channel. A saved selected channel is listened to on application startup. Missing configuration does not trigger fallback to another account. Configuration does not bypass relationship qualification.

After a completed contact invitation, the existing settings surface may record the channel directly with **微信 / QQ / 两个都要**. This closes the previous dead-end where a user could agree to exchange contact details without writing the literal channel name in another letter. A later evidenced letter choice still takes precedence over the settings choice.

Weixin login content is rendered locally as an SVG QR code; successful credentials use the existing Windows DPAPI protection and require one Olivia restart before the listener starts.

QQ remains experimental. The default path is now a managed local component: after the user explicitly chooses QQ and clicks install, Olivia downloads the pinned `NapCat.Shell.Windows.Node.zip` for NapCat `v4.18.28` from the NapNeko GitHub release, verifies its SHA-256, extracts it under local application data, creates a loopback-only OneBot WebSocket on `127.0.0.1:3001`, starts NapCat and opens its local WebUI. After login, the user supplies only the owner QQ; Olivia obtains the bot QQ from `get_login_info`. A manual existing-NapCat path remains available.

NapCat is not included in the Olivia release archive and is governed by its own `Limited Redistribution License for NapCat`, including its upstream non-commercial restriction. The exact source, version and digest are documented in `THIRD_PARTY_NOTICES.md`.

The preview uses the existing persona, memory and private world with a daily-chat presentation. A single structured model response controls text, preferences, appointments and optional stickers. Generation errors have bounded retry; uncertain sends are never blindly retried. Message failures do not stop the listener. Delivered messages enter the existing canonical consumers; consumer failures do not resend the message.

WeChat sends text and occasional relationship-unlocked PNG stickers. Selection favors stickers not recently used and validates eligibility again before sending. Text and image are separate deliveries. Image failure does not roll back delivered text. Windows CNG supplies the protocol-required AES-128-ECB encryption; no additional crypto wheel or local vendor directory is required. QQ retains its existing native voice path when configured.

## Preview limitations

- A newly saved QQ/Weixin binding requires restarting Olivia; hot listener replacement is deliberately outside this change.
- After restart, a new inbound message must establish a live sending target before proactive chat can run. The app must remain running. Persisted appointments do not imply background delivery while the app is closed; expired appointments are not accumulated.
- The managed NapCat download/start/login flow still requires human acceptance on a packaged Windows installation. Automated tests do not prove the upstream binary will behave identically on every Windows machine.
- The Weixin phone-side sticker flow and new proactive deliveries still require human acceptance.
- A user-facing action for resolving uncertain `SENDING` deliveries is not yet supplied.
- Group bots are separate deployments and are not included in this application payload.
- Never ship development account bindings, config files, DPAPI data, `.private`, private letters, or local model credentials.

## Setup security boundary

The setup endpoints require the explicit local companion-action confirmation header and trusted local/original-client origins. A completed contact invitation is required before the settings surface may select a channel. The status endpoint does not return Tencent polling IDs, verification codes, credentials, account IDs or credential paths. QR content is encoded into a local SVG data URL rather than loaded as a remote image.

The managed NapCat source URL, version and archive SHA-256 are pinned. Archive extraction rejects path traversal, symlinks and oversized content. The generated OneBot server binds to `127.0.0.1` only. NapCat's own local OneBot configuration necessarily contains its authentication token; Olivia's normal `personal-chat/config.json` does not. Olivia stores a DPAPI-protected token copy for its own listener startup. Manual QQ configuration accepts a loopback WebSocket endpoint only, requires different character/owner accounts and verifies `get_login_info` before saving.

## Verification

The native binding flow has regression coverage for local QR rendering, setup-state redaction, explicit-action gating, trusted-origin CORS, accepted-channel gating, direct settings channel selection, QQ login verification and plaintext-token exclusion.

The managed QQ component adds tests for the pinned NapCat version/URL/SHA, download-host allowlisting, archive path-traversal rejection, loopback-only OneBot configuration, DPAPI token copy, local WebUI URL construction, managed QQ account discovery and installed-component state. Full Windows public-smoke, repository hardening and offline installer CI remain required before merge.

These checks validate repository wiring and boundaries; they do not substitute for a real Windows user completing NapCat download, QQ login and a live `/连接测试` roundtrip.
