# Local asset manifest

Use the CLI with one or more explicit `alias=path` roots. It reads roots only
and writes a private manifest only when `--output` points inside the ignored
repository `.evidence/` directory:

```text
rtk python tools/asset_manifest.py scan --root original=<local-root> --output .evidence/b01/private-manifest.json
rtk python tools/asset_manifest.py validate --manifest .evidence/b01/private-manifest.json --root original=<local-root>
rtk python tools/asset_manifest.py summary --manifest .evidence/b01/private-manifest.json --output contracts/asset_baseline/asset_manifest.summary.json
```

The private manifest has hashes, source-relative paths, and optional media
probe metadata. The committed summary is count-only by alias, category,
extension, and probe status. The CLI does not print source paths or media
content; repeated SHA-256 values are reported as an informational count.
