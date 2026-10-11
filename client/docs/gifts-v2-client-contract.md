# Gifts v2 (deferred)

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


This branch preserves the existing gift implementation for later work. Purchase feedback, received-gift display and future jewelry grouping remain separate follow-up work.
