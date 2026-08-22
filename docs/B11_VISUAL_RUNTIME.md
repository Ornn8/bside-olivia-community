# B11 LiveTalking visual runtime assembly

B11 adds a replaceable LiveTalking candidate behind the existing B07 visual
driver and B10B lifecycle. The repository contains only the adapter, tests,
manifest metadata and the official-upstream delegation worker. The LiveTalking
checkout, Python environment, checkpoint, avatar payload, original Olivia
reference and generated evidence remain external D:/ or F:/ references.

## Fixed boundary

The runtime is pinned to LiveTalking revision
`a97f01ba366e55eeed94e88d6bae38ed77b3a1b9` from
`https://github.com/lipku/LiveTalking` under Apache-2.0. The B11 worker calls
the upstream Wav2Lip loader, mel extractor, batch inference and paste-back
methods. It does not implement a face model, lip model, renderer or training
path.

The Wav2Lip artifact is accepted only with an official provenance URL, fixed
artifact revision/name, license record and verified SHA-256. The B10B config
stores those fields as references/metadata and never copies them into the
repository.

The official a97f01ba README publishes the LiveTalking Quark share code
`83a750323ef0` and Google Drive folder
`1FOC_MD6wdogyyX_7V1d4NDIO7P9NlSAJ`. The main-process folder inspection
resolved the official Google file IDs for `wav2lip256.pth` and
`wav2lip256_avatar1.tar.gz`; the complete external record is the ignored
`.evidence/b11-runtime/download-manifest.json`. No third-party model mirror is
an accepted source.

## Lifecycle

```text
install core/http + visual-driver + visual-livetalking
enable dependencies in order
customize only D:/ or F:/ runtime/model/avatar/original/evidence references;
optional managed copies record exact source, destination and SHA-256
health -> official dependency, path, payload and checkpoint hash checks
capture -> worker delegates to LiveTalking and writes external PNG evidence
disable -> uninstall -> remove only hash-verified managed destinations recorded
by B10B, while retaining F:/ downloads, original media, avatar payloads and
all other external assets
```

The visual-livetalking health result is fail-closed until every external input
is ready. `external_assets_copied` and `generated_media_committed` remain
false by contract. `external_assets_deleted` is true only when uninstall
removes an exact, SHA-256-verified destination listed in
`managed_external_copies`; `preserve_source=true` keeps the F:/ download and
original Olivia source untouched.

## Avatar preparation

Avatar preparation must invoke the pinned LiveTalking official
`avatars.wav2lip.genavatar` flow against a legal, external Olivia source. The
resulting `data/avatars/<avatar_id>` directory is external and must contain
the upstream `full_imgs`, `face_imgs` and `coords.pkl` payload. Evidence must
record source reference, dimensions, frame timestamps, hashes, upstream
revision, checkpoint URL/revision/SHA/license, dependency versions and GPU
status. Automated compare metrics remain diagnostic; total-control viewing is
required for VIS-01..04.

This pinned upstream writes a `LiveTalking` watermark while extracting
`full_imgs`. After the official flow succeeds, the thin B11 adapter restores
the decoded frames from the same original source video into `full_imgs`; it
does not touch the official face crops, coordinates, mel path, model or
paste-back implementation. This keeps the candidate background and frame
dimensions sourced from Olivia's original video rather than from a generated
or sample background.

The diagnostic input directory is intentionally not synthesized when it is
absent. A run may use only the current canonical original assets and must
record that the supplied diagnostic package was missing rather than reading a
stale baseline package.
