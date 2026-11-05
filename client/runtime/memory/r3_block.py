"""The recalled exchanges in the form the writer reads them (rerank recall).

One JSON envelope, once: the untrusted ``{"untrusted":true,"text":...}`` element (see ``encode``) every later stage
already reads. Inside it are plain lines, not records encoded a second time::

    [ORIGINAL_CORRESPONDENCE_UNTRUSTED]
    recorded_utterance
    [2026-10-10 周六 08:57 · 4周前]
    user: 汤团又挠耳朵了
    linli: 我看它这两天挠得凶，翻翻它爪子看看

What the former record form repeated on every record is said once: the second line holds the evidence kind that all
lines share, the head line holds the program's date, weekday and calendar days since, the tag of a line is its
speaker (the selection note tells the writer how to read it). The citation id, the provenance fields and the scope
field are not shown: nothing after the selection reads them from this text (the trace takes the shown sources from
the selection itself, see ``recall_trace.shown``).

An exchange that carries anything else (media delivery facts, interpretation dependencies, a scope other than a
recorded utterance or a summary) keeps the former record line, so that no fact is lost to the new form. A line of
text that continues on the next line is indented, so a quoted message can never open a head or a speaker line.
"""
from __future__ import annotations

import json

from . import r3_format

MARKER = '[ORIGINAL_CORRESPONDENCE_UNTRUSTED]'
KIND = 'recorded_utterance'
_SPEAKERS = frozenset({'user', 'linli', 'unknown'})
_SCOPES = frozenset({'recorded_utterance', 'retrieved_summary'})
# Every field of a record that _original_evidence_item builds, plus the dated form's own.
_STANDARD = frozenset({'citation', 'provenance', 'speaker', 'occurred_at', 'ago', 'estimated_time',
                       'evidence_scope', 'text', 'when'})


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def encode(text):
    """The envelope around the block text; angle brackets in the recalled text cannot close it."""
    value = _encode({'untrusted': True, 'text': text})
    return '<untrusted_history>' + value.replace('<', r'\u003c').replace('>', r'\u003e') + '</untrusted_history>'


def source(record):
    provenance = record.get('provenance') if isinstance(record, dict) else None
    value = provenance.get('source_record_id') if isinstance(provenance, dict) else None
    return value if isinstance(value, str) and value else None


def _plain(group):
    """The group can be written as dated lines without dropping anything the writer is shown today."""
    if not isinstance(group, list) or not group:
        return False
    for record in group:
        if not isinstance(record, dict) or not set(record) <= _STANDARD:
            return False
        if record.get('evidence_scope') not in _SCOPES or record.get('speaker') not in _SPEAKERS:
            return False
        text = record.get('text')
        if not isinstance(text, str) or not text.strip():
            return False
    return True


def _segments(group):
    """Consecutive records of one source: [(source, records)]. The records of one exchange share a moment."""
    result = []
    for record in group:
        current = source(record)
        if not result or (current is not None and result[-1][0] is not None and current != result[-1][0]):
            result.append([current, [record]])
            continue
        if result[-1][0] is None:
            result[-1][0] = current
        result[-1][1].append(record)
    return result


def _head(records, now):
    for record in records:
        when = r3_format.moment(record.get('occurred_at'))
        if when is not None:
            return '[' + r3_format.label(when, now) + ']', when
    for record in records:
        estimate = record.get('estimated_time')
        if isinstance(estimate, str) and estimate.strip():
            return '[时间不详，推算：' + ' '.join(estimate.split()) + ']', None
    return '[时间不详]', None


def _line(record, when, annotate):
    text = record['text']
    if annotate:
        said = r3_format.moment(record.get('occurred_at')) or when
        if said is not None:
            text = r3_format.annotate(text, r3_format.turn_day(said))
    tag = record['speaker'] + (' summary' if record.get('evidence_scope') == 'retrieved_summary' else '')
    return tag + ': ' + '\n  '.join(text.splitlines())


def lines(groups, now, *, annotate=False, known_sources=(), legacy=None):
    """The lines under the two header lines. ``annotate`` resolves day words (the dated format's switch),
    ``known_sources`` are the sources whose originals the writer reads elsewhere (a summary of them adds nothing),
    ``legacy`` shapes a group that keeps the former record line."""
    originals = {source(r) for group in groups if isinstance(group, list) for r in group
                 if isinstance(r, dict) and r.get('evidence_scope') == 'recorded_utterance' and r.get('text')}
    originals = (originals | set(known_sources)) - {None}
    result = []
    for group in groups:
        if not _plain(group):
            result.append(_encode(legacy(group) if legacy else group))
            continue
        for origin, records in _segments(group):
            shown = [r for r in records if not (r['evidence_scope'] == 'retrieved_summary' and origin in originals)]
            if not shown:
                continue
            head, when = _head(shown, now)
            result.append(head)
            result.extend(_line(r, when, annotate) for r in shown)
    return result


def text(groups, now, **options):
    return '\n'.join([MARKER, KIND, *lines(groups, now, **options)])


def block(groups, now, **options):
    """The whole untrusted element, the thing whose length the recall cap bounds."""
    return encode(text(groups, now, **options))


def is_compact(block_text):
    """The decoded text of a recalled block is in this form (its second line is the shared evidence kind)."""
    head = block_text.split('\n', 2)
    return len(head) >= 2 and head[0] == MARKER and head[1] == KIND


def units(all_lines):
    """[(first, end)] line ranges of the exchanges: a head line (or a former record line) up to the next one."""
    starts = [i for i, line in enumerate(all_lines) if i >= 2 and line.startswith('[')]
    return list(zip(starts, starts[1:] + [len(all_lines)]))
