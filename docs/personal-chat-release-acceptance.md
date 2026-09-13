# Personal chat preview and relationship fixes

## Shipping scope

Meaningful exchanges and shared experiences now contribute bounded, evidence-backed relationship growth. Rewritten replies and reclassified identical user evidence do not award daily growth twice. A high relationship does not itself grant a romantic stage or intimacy permission.

Personal QQ and official WeChat integration are a **preview**, disabled in a normal installation. They require explicit local configuration, credentials, a single permitted owner, and accepted contact access. A saved selected channel is listened to on application startup. Missing configuration does not trigger fallback to another account. Contact invitations are suppressed without an explicit configuration file; configuration does not bypass relationship qualification.

The preview uses the existing persona, memory and private world with a daily-chat presentation. A single structured model response controls text, preferences, appointments and optional stickers. Generation errors have bounded retry; uncertain sends are never blindly retried. Message failures no longer stop the listener. Delivered messages enter the existing canonical consumers; consumer failures do not resend the message.

WeChat sends text and occasional relationship-unlocked PNG stickers. Selection favors stickers not recently used and validates eligibility again before sending. Text and image are separate deliveries. Image failure does not roll back delivered text. Windows CNG supplies the protocol-required AES-128-ECB encryption; no additional crypto wheel or local vendor directory is required. QQ retains its existing native voice path when configured.

## Preview limitations

- No native first-time QR/account-binding interface is supplied in this release. Configuration and connection probes remain required.
- After restart, a new inbound message must establish a live sending target before proactive chat can run. The app must remain running. Persisted appointments do not imply background delivery while the app is closed; expired appointments are not accumulated.
- WeChat phone-side sticker rendering and new proactive deliveries have not received human acceptance. Local health and automated tests do not establish this.
- Group bots are separate deployments and are not included in this application payload.
- Never ship development account bindings, config files, DPAPI data, `.private`, private letters, or local model credentials.

## Verification

Before the final packaging fix, 383 targeted tests passed across personal transports, recovery, relationship growth, canonical memory consumers, reply orchestration, provider request policy and payload exclusion. Windows CNG encryption subsequently passed known-vector, block-padding and independent-library comparisons alongside image transport and payload tests (21 tests). Preview gating and related tests passed (20 tests). The release integration must run its own checks against the final merged commit.

Six isolated synthetic high-relationship Flash/high scenarios produced valid structured decisions after one provider failure was retried. Closing, temporary quiet hours, a requested next-day appointment, channel limits, avoiding letter invitations and a serious long message were covered. These calls neither sent live messages nor wrote real user memory.

Microsoft CNG API reference: https://learn.microsoft.com/windows/win32/api/bcrypt/nf-bcrypt-bcryptencrypt
