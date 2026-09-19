# B01B visual baseline contract

This batch creates an original-only visual truth baseline and repeatable
acceptance tooling. It does not generate a candidate digital human and does
not claim that appearance has been reproduced.

## Source and privacy boundary

The B01A `private_asset_manifest` is the only source registry. Commands select
assets by `asset_<sha256-prefix>` logical ID plus an explicit timestamp; source
paths are never accepted as selectors and are never written to committed
summaries. Original roots, packaged container files, unpacked front-end files,
player HTML, frames, contact sheets, and hashes stay below ignored
`.evidence/`.

The submitted files are the tool, schemas, synthetic example, tests, and
count/state-only summary. No original media, player data, source-relative
filename, absolute path, source hash, or user data belongs in the candidate
diff.

## State matrix

`visual_state_matrix` covers `day`, `dusk`, `night`, `idle`,
`piano_performance`, `letter_reply`, `letter_reading`, `live`,
`outfit_variants`, and `scene_transitions`. Each unit has `status`,
`evidence_count`, `required_shots`, and `notes_code`.

`CANDIDATE` means a manifest logical ID was selected from a path/name signal.
It is not an acceptance state. A unit may become `UNVERIFIED` after frames are
extracted, but it can become `VERIFIED` only with enough evidence and an
explicit `verification_method=manual_review`; filename inference can never
verify it.

## Private CLI

```text
rtk python tools/visual_baseline.py candidates --manifest .evidence/b01b/run/private-manifest.json --root game=<local-root> --root olivia=<local-root> --root unpacked=<local-root> --root player=<local-root> --output .evidence/b01b/run/state-candidates.json

rtk python tools/visual_baseline.py extract-batch --manifest .evidence/b01b/run/private-manifest.json --root game=<local-root> --root olivia=<local-root> --root unpacked=<local-root> --root player=<local-root> --shot day=asset_<id>@0.0 --shot dusk=asset_<id>@0.0 --output-dir .evidence/b01b/run/frames --frame-index .evidence/b01b/run/frame-index.json --contact-sheet .evidence/b01b/run/contact-sheet.png

rtk python tools/visual_baseline.py matrix --manifest .evidence/b01b/run/private-manifest.json --root game=<local-root> --root olivia=<local-root> --root unpacked=<local-root> --root player=<local-root> --frame-index .evidence/b01b/run/frame-index.json --output .evidence/b01b/run/state-matrix.json --summary contracts/asset_baseline/visual_baseline.summary.json

rtk python tools/visual_compare.py compare --reference .evidence/b01b/run/frames/frame_0001.png --candidate .evidence/b01b/run/frames/frame_0002.png --output .evidence/b01b/run/comparison.json
```

The extractor uses OpenCV when installed, writes same-resolution PNGs and
sidecar metadata, and creates a private contact sheet. Missing ffmpeg/ffprobe
only removes probe metadata for formats that need those tools; OpenCV video
decoding remains a separate capability. The comparison command reports
`MEASURED`, `UNAVAILABLE`, or `UNVERIFIED` per metric. It includes exact pixel
diff, dimensions/alpha, PSNR, optional SSIM/LPIPS, colour difference,
sharpness, identity hook, background drift, temporal flicker, frame rate, and
A/V sync metadata. Thresholds remain `UNFROZEN`; no metric is a PASS signal.

Total-control viewing of original and candidate frames remains mandatory. This
batch contains only original baseline frames, so it cannot establish candidate
identity, face shape, hair, skin tone, outfit, composition, background,
lighting, or clarity equivalence.
