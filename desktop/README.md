# Olivia for macOS

Requires macOS 14 or later. The default arm64 and x64 DMGs use a complete local ad-hoc signature and are not notarized. `codesign --verify --deep --strict` must pass; Gatekeeper still does not trust these as Developer ID releases. First launch may require approval in System Settings → Privacy & Security. Do not remove the signature: Apple Silicon can terminate an unsigned executable immediately. For a Developer ID build, set `OLIVIA_MAC_IDENTITY` to the exact signing identity before running the build command. Build on an Apple Silicon Mac with Rosetta 2 installed for the x64 validation run. The app loads the verified original Windows 0.0.9.627 frontend and applies the repository's Windows compatibility patches at startup; it does not redraw the interface.

```sh
cd desktop
npm ci
npm run dist:arm64
npm run dist:x64
```

The DMG never contains the Windows original client or extracted assets. On first packaged launch, Olivia asks you to choose a locally available directory containing the privately obtained original files, then copies only the required files into `~/Library/Application Support/BSide Olivia/Original`; it does not upload or package them. During development, set `OLIVIA_ORIGINAL_ASSET_ROOT`. Do not publish original files or a DMG that includes them without distribution rights. Each build downloads a checksum-pinned standalone Python 3.12 runtime, installs pinned dependencies for that architecture, and verifies the pinned MIT-licensed BGE embedding model. The app bundles FFmpeg through `imageio-ffmpeg`. Output is in `desktop/dist/`.

On first launch, the app creates `~/Library/Application Support/BSide Olivia/Data`. The model is copied and verified there. Account, WeChat, and QQ secrets are stored in macOS Keychain, with only opaque references in the data directory. Window close keeps Olivia visible in the menu bar; Quit stops the backend. QQ expects a separately running local OneBot endpoint. Windows history is not imported automatically.

Olivia uses a single-instance lock for the Application Support profile. The menu bar item remains available after the window is closed, and its context menu shows the explicit proactive-contact and chat connection state. The original client's `/ws` channel is proxied through the authenticated local backend and accepts a real HTTP `101 Switching Protocols` upgrade. If the backend exits after startup, Olivia retries once and then presents an explicit retry/quit dialog. A Keychain read failure is surfaced as `LLM_SETUP_KEY_UNAVAILABLE` with `keychain_status: UNAVAILABLE`; it is not treated as an unconfigured account.

The Mac QQ settings open the manual connection form and omit the Windows-only NapCat installer. Run your own local OneBot service, enable its forward WebSocket server on the loopback address, and enter the character QQ, your own QQ, URL and token. The setup probes the logged-in QQ before saving. Native route notifications are not echoed back as navigation commands, and explicit navigation preserves the private entry pathname through hash routes. Quit closes both HTTP and upgraded WebSocket connections before stopping the backend.
