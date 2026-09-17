# Personal chat preview and relationship fixes

## Shipping scope

Meaningful exchanges and shared experiences now contribute bounded, evidence-backed relationship growth. Rewritten replies and reclassified identical user evidence do not award daily growth twice. A high relationship does not itself grant a romantic stage or intimacy permission.

Personal QQ and official WeChat integration are a **preview**, disabled until the user has accepted the corresponding contact channel. A saved selected channel is listened to on application startup. Missing configuration does not trigger fallback to another account. Configuration does not bypass relationship qualification.

The existing settings surface now includes a first-time binding flow after a channel has been selected. Official Weixin login content is rendered locally as an SVG QR code; successful credentials use the existing Windows DPAPI protection and require one Olivia restart before the listener starts. QQ remains experimental and requires a separately installed NapCat OneBot endpoint. The settings form verifies the local QQ login before saving an encrypted token reference. Plaintext QQ tokens are not written to `config.json`.

The preview uses the existing persona, memory and private world with a daily-chat presentation. A single structured model response controls text, preferences, appointments and optional stickers. Generation errors have bounded retry; uncertain sends are never blindly retried. Message failures no longer stop the listener. Delivered messages enter the existing canonical consumers; consumer failures do not resend the message.

WeChat sends text and occasional relationship-unlocked PNG stickers. Selection favors stickers not recently used and validates eligibility again before sending. Text and image are separate deliveries. Image failure does not roll back delivered text. Windows CNG supplies the protocol-required AES-128-ECB encryption; no additional crypto wheel or local vendor directory is required. QQ retains its existing native voice path when configured.

## Preview limitations

- A newly saved QQ/Weixin binding requires restarting Olivia; hot listener replacement is deliberately outside this change.
- After restart, a new inbound message must establish a live sending target before proactive chat can run. The app must remain running. Persisted appointments do not imply background delivery while the app is closed; expired appointments are not accumulated.
- The packaged Windows UI binding flow, WeChat phone-side sticker rendering and new proactive deliveries still require human acceptance. Local health and automated tests do not establish this.
- A user-facing action for resolving uncertain `SENDING` deliveries is not yet supplied.
- Group bots are separate deployments and are not included in this application payload.
- Never ship development account bindings, config files, DPAPI data, `.private`, private letters, or local model credentials.

## Setup security boundary

The setup endpoints require the explicit local companion-action confirmation header and reject any channel that is not present in the user's evidenced contact choice. The status endpoint does not return Tencent polling IDs, verification codes, credentials, account IDs or credential paths. QR content is encoded into a local SVG data URL rather than loaded as a remote image. QQ accepts a loopback WebSocket endpoint only, requires different character/owner accounts, checks `get_login_info`, and stores only a DPAPI-protected token file reference in configuration.

## Verification

Before the final packaging fix, 383 targeted tests passed across personal transports, recovery, relationship growth, canonical memory consumers, reply orchestration, provider request policy and payload exclusion. Windows CNG encryption subsequently passed known-vector, block-padding and independent-library comparisons alongside image transport and payload tests (21 tests). Preview gating and related tests passed (20 tests). The release integration must run its own checks against the final merged commit.

The native binding change adds regression coverage for local QR rendering, setup-state redaction, explicit-action gating, accepted-channel gating, QQ login verification and plaintext-token exclusion, plus the existing settings archive upgrade/idempotency checks.

Six isolated synthetic high-relationship Flash/high scenarios produced valid structured decisions after one provider failure was retried. Closing, temporary quiet hours, a requested next-day appointment, channel limits, avoiding letter invitations and a serious long message were covered. These calls neither sent live messages nor wrote real user memory.

Microsoft CNG API reference: https://learn.microsoft.com/windows/win32/api/bcrypt/nf-bcrypt-bcryptencrypt
