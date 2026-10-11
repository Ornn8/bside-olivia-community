# Windows native shortcut launcher

The managed install root contains `Olivia.exe` and `olivia.ico`. The desktop and
Start Menu shortcuts are named `Olivia`. The small .NET Framework WinExe starts
the sibling embedded runtime's `pythonw.exe`, waits for the stable Python
launcher, and reports failures with a Windows message box. Exit codes `0` and
`0xffffffff` are silent. Missing Python or launcher files return `2`.

`START.cmd`, `START.vbs`, the repair entrypoint, and proactive login remain
available. The executable inherits `OLIVIA_PORT`, defaulting to `8899`, and
passes that value both as `--port` and in the child environment.

## Build and integrity

From the client directory:

```powershell
python -m installer.build_native_launcher --output build/native/Olivia.exe
# Optional compiler override for either packaging entrypoint:
$env:OLIVIA_CSC = 'X:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
python -m installer build-update --source . --output build/update.oliviapatch --version 2.2.16 --source-commit <commit>
```

The standalone builder also accepts `--source` and `--csc`. Setup and component
builders share it. Compiler temporary files stay beside the output. Framework
csc does not implement `/deterministic`; the builder disables debug information,
fixes the output assembly name, clears the PE timestamp, and replaces its single
MVID with a hash of the normalized image. An unexpected GUID heap fails closed.
Two builds in different directories must produce identical bytes with the same
compiler, source, manifest, and icon. There is no signing step.

The generated payload contains `installer/olivia_launcher/Olivia.exe` and its
SHA-256 sidecar. Both enter the component manifest with size and SHA-256. The
installer checks the executable's sidecar before publishing the root entrypoint.
Setup's positive file allowlist includes both generated files; its tracked build
inputs include the C# source, manifest and builder. Uninstall and managed refresh
ownership lists include the root executable and icon.

## Migration and isolated checks

Shortcut refresh visits desktop and Programs independently. It checks each
known shortcut's target before changing or removing it. Owned CMD/VBS links are
replaced with the native target; the old localized filename is removed only
after the replacement is saved. A custom target, conflicting custom destination,
or read-only shortcut is preserved. Refresh returns failed paths and continues
with the other candidates. It does not recreate deleted shortcuts.

Shortcut I/O uses the native Unicode
[IShellLinkW interface](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nn-shobjidl_core-ishelllinkw).
This avoids WSH shortcut-property failures with Chinese paths on English Windows.

Tests can pass `-DesktopPath`, `-ProgramsPath`, or `-ShortcutPath`. Entire update
and uninstall flows can inherit `OLIVIA_SHORTCUT_DESKTOP` and
`OLIVIA_SHORTCUT_PROGRAMS`; set both to temporary directories to avoid real shell
folders.

The 2.2.15 updater executes its already-installed shortcut script after switching
the backend pointer. It cannot execute this new migration immediately. The new
backend therefore also performs migration on its first startup. Subsequent
updates using the new updater migrate during shortcut refresh. An immediate
2.2.15 migration requires a separately invoked new installer/update entrypoint;
the component package alone does not provide that guarantee.
