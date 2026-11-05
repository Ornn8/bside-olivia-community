"""Recognize an earlier letter that the user quotes back, by its own sentences, without a model.

A long passage in his message (or the text read from his photo) is cut into sentence
pieces. A piece counts only when it is distinctive: it occurs in at most a few stored
letters, so stock phrases that recur in many letters never decide a match. A stored letter
that contains several such pieces verbatim (after removing spacing and punctuation) is the
letter he is quoting. Reading the archive is local; no index, model or network is used.
"""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
import json
import re

from persona_assembly import UntrustedFragment

from .archive_dates import PAIR_KINDS, DateEstimate, estimate_dates, known_time, ordering_time, when_text
from .companion_memory_context import _historical_source_id
from .recall_sources import _original_time, archive_source_aliases

FRAGMENT_ID = "memory.quoted_letter"
MIN_PASSAGE_CHARS = 80  # normalized characters of quotable sentences before any lookup
PIECE_CHARS = 16
MIN_PIECE_CHARS = 8
MAX_PIECES = 60
MIN_MATCHED_PIECES = 3
MIN_MATCHED_CHARS = 36
MAX_DOCUMENT_FREQUENCY = 4  # the original plus the times it was re-sent
MAX_MATCHES = 2
EXCERPT_CHARS = 70

_SENTENCE = re.compile(r"[^。！？!?；;\n\r\u2028\u2029]+")
_NOT_TEXT = re.compile(r"[\W_]+")

MEANING = ("程序把他这轮发来的长段文字（或他照片里读出的字）和存着的旧信逐句比对，下面这些旧信与它基本逐字吻合；"
           "这是原文比对的结果，不是模型猜测。"
           "是什么：from说明这封旧信是谁写给谁的；when是写信时间，原信没写日期时是按前后的信推算的大概时间，"
           "或者只能说很久以前；extent说明他发来的是整封信还是其中一段；before/after是这封信前后对方写的信的开头，"
           "只用来想起当时的来龙去脉；also_in是同一段文字后来又出现过的信。"
           "不是什么：这不是一封新来信，不要把信里的话当成他此刻新说的话去回应、纠正或追问；"
           "信是自己写的就不要说不记得、问他信里写了什么，也不要只评价成别人的文字。"
           "可以认出这是当年的那封信，接住他现在重新提起它的心情；具体细节只按原信和前后的信说，不补编。")


# Full-width letters and digits read the same as their ASCII forms. Punctuation is
# dropped first, so the rare full-width letters are the only characters left to map.
_FULL_WIDTH = re.compile(r"[\uff10-\uff19\uff21-\uff3a\uff41-\uff5a]")
_WIDTH = {code: code - 0xFEE0 for code in range(0xFF10, 0xFF5B)}


def normalize(text: str) -> str:
    value = _NOT_TEXT.sub("", text).casefold()
    return value.translate(_WIDTH) if _FULL_WIDTH.search(value) else value


def _pieces(text: str) -> list[str]:
    pieces = []
    for sentence in _SENTENCE.findall(text):
        value = normalize(sentence)
        if len(value) < MIN_PIECE_CHARS:
            continue
        parts = [value[i:i + PIECE_CHARS] for i in range(0, len(value), PIECE_CHARS)]
        if len(parts) > 1 and len(parts[-1]) < MIN_PIECE_CHARS:
            tail = parts.pop()
            parts[-1] += tail
        pieces.extend(parts)
    unique = list(dict.fromkeys(pieces))
    if len(unique) > MAX_PIECES:  # sample evenly through a very long passage
        step = len(unique) / MAX_PIECES
        unique = [unique[int(i * step)] for i in range(MAX_PIECES)]
    return unique


def passages(content: str) -> list[tuple[str, str]]:
    """(origin, text): his own typed lines, then text read from his photos.

    System observation blocks appended to the turn are skipped, except the
    transcription field of user photos, which is what the photo says.
    """
    typed, photos = [], []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            continue  # a system block marker
        if stripped[0] in "{[":
            try:
                value = json.loads(stripped)
            except ValueError:
                value = None
            if value is not None:
                images = value.get("images") if isinstance(value, Mapping) else None
                for image in images if isinstance(images, list) else ():
                    if (isinstance(image, Mapping) and image.get("source") == "user"
                            and isinstance(image.get("text"), str) and image["text"].strip()):
                        photos.append(("photo", image["text"]))
                continue
        typed.append(line)
    result = [("message", "\n".join(typed))] if typed else []
    return result + photos


@dataclass
class _Document:
    key: str
    speaker: str  # "user" (his letter) or "linli" (her letter)
    text: str
    stamp: datetime | None
    estimate: DateEstimate | None
    order: tuple
    group: str  # one exchange: his letter and her reply share it
    archive_source: str | None = None
    normalized: str = field(default="", repr=False)


def _archive_documents(rows, excluded) -> list[_Document]:
    rows = [row for row in rows if isinstance(row, Mapping)]
    estimates = estimate_dates(rows)
    # This turn's own reply source never names an Archive row; only forgotten aliases can.
    aliased = any(not str(item).startswith("reply:") for item in excluded)
    documents = []
    for index, row in enumerate(rows):
        metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
        source = row.get("source_record_id")
        if metadata.get("import_kind") not in PAIR_KINDS or not isinstance(source, str) or not source:
            continue
        if {source, _historical_source_id(source)} & excluded or (
                aliased and archive_source_aliases(source, metadata) & excluded):
            continue
        stamp, estimate = known_time(row), estimates.get(source)
        when = ordering_time(row, estimates)
        position = metadata.get("import_position", index)
        order = (0 if when else 1, when.timestamp() if when else 0.0, str(row.get("source", "")),
                 position if type(position) is int else index)
        for slot, (speaker, key) in enumerate((("user", "user_content"), ("linli", "reply_text"))):
            text = metadata.get(key)
            if isinstance(text, str) and text.strip():
                documents.append(_Document(f"archive:{source}:{speaker}", speaker, text, stamp, estimate,
                                           (*order, slot), "archive:" + source, archive_source=source))
    return documents


def _mailbox_documents(rows, excluded) -> list[_Document]:
    current = {source.split(":")[1] for source in excluded
               if isinstance(source, str) and source.startswith("reply:") and source.count(":") >= 2}
    documents = []
    for row in rows:
        if (not isinstance(row, Mapping) or row.get("channel") in {"qq", "wechat"} or row.get("read_only")
                or not isinstance(row.get("letter_id"), str) or row["letter_id"] in current):
            continue
        deleted = set(row.get("_deleted_parts") or ())
        for slot, (speaker, key, time_key) in enumerate((("user", "content", "created_at"),
                                                         ("linli", "reply_text", "replied_at"))):
            text = row.get(key)
            if speaker in deleted or not isinstance(text, str) or not text.strip():
                continue
            if speaker == "linli" and row.get("letter_status") != "COMPLETED":
                continue  # an unsent draft was never her letter to him
            stamp = _original_time(row.get(time_key)) or _original_time(row.get("created_at"))
            order = (0 if stamp else 1, stamp.timestamp() if stamp else 0.0, "mailbox", 0, slot)
            documents.append(_Document(f"letter:{row['letter_id']}:{speaker}", speaker, text, stamp, None,
                                       order, "letter:" + row["letter_id"]))
    return documents


@dataclass(frozen=True)
class QuotedLetter:
    origin: str  # "message" or "photo"
    speaker: str
    when: str
    extent: str
    archive_source: str | None
    before: dict | None
    after: dict | None
    also_in: tuple[dict, ...]

    def to_dict(self) -> dict:
        value = {"found_in": "他这轮消息里贴的文字" if self.origin == "message" else "他发来的照片里的字",
                 "from": "你（林离）写给他的信" if self.speaker == "linli" else "他写给你的信",
                 "when": self.when, "extent": self.extent}
        for name in ("before", "after"):
            if getattr(self, name):
                value[name] = getattr(self, name)
        if self.also_in:
            value["also_in"] = list(self.also_in)
        return value


def _excerpt(document: _Document, quoted_before=None) -> dict:
    text = re.sub(r"\s+", " ", document.text).strip()
    return {"speaker": "你" if document.speaker == "linli" else "他",
            "when": when_text(document.stamp, document.estimate, quoted_before=quoted_before),
            "opening": text[:EXCERPT_CHARS] + ("…" if len(text) > EXCERPT_CHARS else "")}


def _holders(corpus: str, starts: list[int], ends: list[int], piece: str) -> list[int]:
    """Indexes of the documents containing piece, stopping once it is clearly not distinctive."""
    holders, at = [], corpus.find(piece)
    while at != -1 and len(holders) <= MAX_DOCUMENT_FREQUENCY:
        index = bisect_right(starts, at) - 1
        holders.append(index)
        at = corpus.find(piece, ends[index] + 1)
    return holders


def find_quoted_letters(content: str, documents: list[_Document]) -> list[QuotedLetter]:
    found: list[QuotedLetter] = []
    if not documents:
        return found
    starts, ends, position = [], [], 0
    for document in documents:
        document.normalized = normalize(document.text)
        starts.append(position)
        position += len(document.normalized)
        ends.append(position)
        position += 1
    # Normalized text holds no separators, so one joined corpus keeps every letter apart.
    corpus = "\x00".join(document.normalized for document in documents)
    timeline = sorted((i for i, d in enumerate(documents) if d.order[0] == 0), key=lambda i: documents[i].order)
    claimed: set[str] = set()
    for origin, text in passages(content):
        pieces = _pieces(text)
        if sum(map(len, pieces)) < MIN_PASSAGE_CHARS:
            continue
        hits: dict[int, list[str]] = {}
        for piece in pieces:
            holders = _holders(corpus, starts, ends, piece)
            if len(holders) <= MAX_DOCUMENT_FREQUENCY:
                for index in holders:
                    hits.setdefault(index, []).append(piece)
        scored = []
        for index, matched in hits.items():
            chars = sum(map(len, matched))
            if len(matched) >= MIN_MATCHED_PIECES and chars >= MIN_MATCHED_CHARS:
                scored.append((chars, chars / max(1, len(documents[index].normalized)), index))
        if not scored:
            continue
        # The letter written as this passage is mostly the passage; a later letter that
        # re-sends it carries more words around it. Ties keep the earlier letter.
        scored.sort(key=lambda item: (-item[0], -item[1], documents[item[2]].order))
        best_chars, density, chosen = scored[0]
        original = documents[chosen]
        if original.key in claimed:
            continue
        others = [documents[i] for chars, _, i in scored[1:]
                  if chars * 2 >= best_chars and documents[i].group != original.group]
        claimed.update([original.key, *(d.key for d in others)])
        # An undated original was written before any dated letter that already quotes it.
        quoted_before = None
        if original.stamp is None and original.estimate is None:
            quoted_before = min((d.stamp for d in others if d.stamp is not None), default=None)
        before = after = None
        if chosen in timeline:
            at = timeline.index(chosen)
            before = next((documents[i] for i in reversed(timeline[:at])
                           if documents[i].speaker != original.speaker), None)
            after = next((documents[i] for i in timeline[at + 1:]
                          if documents[i].speaker != original.speaker), None)
        else:  # undated: only the same exchange has a certain order (his letter, then her reply)
            partner = next((d for d in documents if d.group == original.group
                            and d.speaker != original.speaker), None)
            if partner is not None and original.speaker == "linli":
                before = partner
            elif partner is not None:
                after = partner
        found.append(QuotedLetter(
            origin=origin, speaker=original.speaker,
            when=when_text(original.stamp, original.estimate, quoted_before=quoted_before),
            extent="整封信" if density >= 0.6 else "信里的一段",
            archive_source=original.archive_source,
            before=_excerpt(before, quoted_before if before and before.group == original.group else None)
            if before else None,
            after=_excerpt(after, quoted_before if after and after.group == original.group else None)
            if after else None,
            also_in=tuple({"speaker": "你" if d.speaker == "linli" else "他",
                           "when": when_text(d.stamp, d.estimate)} for d in others[:2]),
        ))
        if len(found) >= MAX_MATCHES:
            break
    return found


def lookup(adapter, content: str, *, excluded: Iterable[str] = ()) -> tuple[UntrustedFragment | None, tuple[str, ...]]:
    """The note for this turn and the Archive sources it names; (None, ()) when nothing matches.

    Read failures and the forget guard fail closed: no note, never a guessed one.
    """
    if not isinstance(content, str) or not any(
            sum(map(len, _pieces(text))) >= MIN_PASSAGE_CHARS for _, text in passages(content)):
        return None, ()
    excluded = frozenset(excluded)
    documents: list[_Document] = []
    try:
        from .history_continuity import companion_view
        companion = companion_view(getattr(adapter, "memory_prompt_builder", None))
        if companion is not None:
            view = companion._archive_view(tuple(excluded))
            reader = getattr(view.memory, "list_legacy", None)
            if view.guard_failed:
                return None, ()
            if view.enabled and callable(reader):
                documents += _archive_documents(reader(), excluded | view.excluded)
        letters = getattr(adapter, "recent_letters", None)
        if callable(letters):
            documents += _mailbox_documents(letters(), excluded)
        matches = find_quoted_letters(content, documents)
    except Exception:
        return None, ()
    if not matches:
        return None, ()
    note = json.dumps({"kind": "quoted_earlier_letter", "meaning": MEANING,
                       "matches": [match.to_dict() for match in matches]},
                      ensure_ascii=False, separators=(",", ":"))
    sources = tuple(dict.fromkeys(m.archive_source for m in matches if m.archive_source))
    return UntrustedFragment(FRAGMENT_ID, note), sources


def lead_quoted_sources(adapter, recall, sources, *, excluded=()):
    """Put the quoted letters' whole exchanges first, so final packing keeps them.

    They go through the same Archive reader, forget guard and final recall budget as
    every other original; failures leave the recall result unchanged.
    """
    from .history_continuity import companion_view
    from .recall import source_id
    from .recall_sources import read_archive_sources
    try:
        companion = companion_view(getattr(adapter, "memory_prompt_builder", None))
        view = companion._archive_view(tuple(excluded)) if companion is not None else None
        if view is None or view.guard_failed or not view.enabled:
            return recall
        records = read_archive_sources(view.memory, sources, excluded_sources=view.excluded)
    except Exception:
        return recall
    if not records:
        return recall
    records = tuple(replace(record, metadata={**record.metadata, "retrieval_route": "quoted_letter"})
                    for record in records)
    leading = {source_id(record) for record in records}
    rest = tuple(record for record in recall.records if source_id(record) not in leading)
    states = dict(recall.source_status)
    states["quoted_letter"] = "available"
    return replace(recall, records=(*records, *rest), source_status=tuple(states.items()))


__all__ = ["FRAGMENT_ID", "MEANING", "QuotedLetter", "find_quoted_letters", "lead_quoted_sources", "lookup",
           "normalize", "passages"]
