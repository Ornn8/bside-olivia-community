# Letter illustrations

54 user-approved Linli chibi line illustrations, 512 x 512 RGBA PNG.
Generated with built-in imagegen from the approved character reference, then deterministically split and white-matte removed with explicit user authorization. `catalog.json` maps stable IDs to Chinese labels and source-sheet cells.

`selection.py` filters the 54 illustrations using the trusted relationship view: 29 base illustrations, 45 when familiarity is medium/high or the confirmed stage is familiar/close/committed, all 54 when familiarity plus trust and comfort reach medium. Unknown values never qualify as high. Availability is a snapshot per reply, not a permanent change to character permissions.

The existing text-letter generation call receives only eligible IDs and labels and returns a reserved `[[sticker:linli-NN]]` footer after the letter. The pipeline removes it before review, TTS, memory and world extraction. Invalid/locked/missing IDs use base linli-01; a rewritten letter also falls back instead of making a second call. Prompt space is reserved within the existing input budget. No additional LLM call is made, but eligible descriptions add input tokens. This presentation protocol is separate from the persona asset.

The accepted ID is persisted as reply_sticker_id and exposed as replyStickerId only after publication. `select.js` only validates that persisted ID for rendering; old letters without metadata get the base illustration. It no longer matches keywords. Model semantic quality still needs live-response acceptance; automated tests exercise the protocol with synthetic provider outputs.

`installer/patch_letter_stickers.py` modifies the native 0627 reply component and bundles these assets inside feapp.dat. It reserves footer space at lower left, includes the illustration in native PNG capture, and expands long text only during capture. Empty/error/video-only cards do not gain a sticker. Text cards with voice controls do. Original line breaks and signature text are preserved; this slice does not generate a signature.

Validation: 54 real-alpha images; deterministic selection and sensitive-topic precedence; atomic rejection of unknown anchors; idempotency and preservation of other archive members. Native component browser smoke checks cover short/long text, voice, empty and error cards. No new live LLM calls were used for this change.
