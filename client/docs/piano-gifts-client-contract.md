# Piano and gifts client integration

The client integrates the optional server piano capability through the existing asset and task APIs. There is no local synthesis, MIDI rewriting, or new speech format. Missing or disabled capabilities keep the feature hidden. Unknown capability/catalog fields are ignored.

## Piano

- `piano.enabled` must be exactly `true`. Library IDs, duration limits, the MIDI size limit, and original-composition availability come from the capability response.
- QQ accepts `.mid` and `.midi` only for an enabled capability. It bounds the download, checks `MThd`, and uploads the unchanged file through `/v1/assets`. The returned asset ID must be the file SHA-256. Invalid files get a natural-language explanation in the current reply context.
- The decision request advertises a top-level `piano_experience` object containing the normalized capability plus `midi_available` for this turn. The decision response may contain `piano_request`; when advertised, this slot is required and can be null. The frozen record retains both capability and request.
- `piece_hint` is matched against an exact library title or ID. An unknown hint does not silently select another piece. Original requests use the bounded hint as title/mood. A MIDI decision cannot execute without a validated file in the same input revision. A direct MIDI upload defaults to a short recording within the server limit.
- The writer sees the resolved title/composer and target duration, and writes only a brief acknowledgement. It is explicitly instructed not to claim recording or delivery has already finished. The current submit contract does not name a section; the client does not invent one before the result arrives.
- Task receipts retain the original request ID and payload across restart and uncertain transport. Damaged receipts fail closed. A piano result is not acknowledged to the server until QQ confirms file acceptance.
- QQ sends an MP3 file, then a brief description derived from validated `piano_meta`. Ambiguous file delivery never automatically resends. Only confirmed delivery becomes a QQ memory fact.
- Native completion is recorded even if the panel is closed. Its persisted job records feed the existing memory outbox; deleting a visible job does not delete the completion memory. Confirmed recent native/QQ recordings are also available in the next writer context while the outbox catches up.

See [native-piano-contract.md](native-piano-contract.md) for the original player investigation and native route shapes. The enhanced original entry uses an audio player and MP3 download because the shipped video preview hardcodes an MP4 type.

## Initial gift catalog proposal

The server owns descriptions, amounts, availability, ownership, and repeatability. These are the initial requested IDs and semantics; the UI does not embed a commercial catalog.

| ID | Category | Display name | Repeatable | Custom title |
| --- | --- | --- | --- | --- |
| espresso-machine | home | 意式咖啡机 | No | No |
| blanket | home | 毯子 | No | No |
| necklace | jewelry | 项链 | No | No |
| tuning-fork | music | 音叉 | No | No |
| vinyl-record | music | 黑胶 | Yes | Album name |
| sheet-music | music | 乐谱 | Yes | Collection name |
| flowers | consumable | 鲜花 | Yes | No |

The gift panel retains cameras and adds groups only when `items` is present. Custom titles contain 1–60 characters. The purchase endpoint checks the current catalog before accepting a custom title. Every explicit repeat purchase goes to the server; the client does not locally infer free purchases from ownership. Unknown purchase outcomes are not automatically retried.

Owned gifts and each distinct custom title are retained in local world state. Album and score names are labels supplied by the user, not knowledge of their contents. The existing decision-controlled search remains responsible for factual lookup. Jewelry rendering belongs to the server.

## Server integration decisions

1. Confirm the decision-side `piano_experience` negotiation and current-turn `midi_available` flag; they are needed to keep old servers and ordinary file turns unchanged.
2. Confirm the seven catalog IDs and their server-owned semantics, including how repeated purchases of different albums/collections appear in subsequent catalog reads.
3. Confirm section selection. If the writer must name the exact section before recording, the server contract needs a planning value or a documented deterministic section policy; current task input only supplies source and duration.
4. Piano currently inherits the existing generic task charge-consent header. Confirm its compatibility before enabling the capability; no new amount is embedded by this change.

Mock HTTP, writer, delivery, and browser tests establish client behavior only. Real server generation, sound quality, jewelry in generated media, and actual QQ delivery require the server integration run.
