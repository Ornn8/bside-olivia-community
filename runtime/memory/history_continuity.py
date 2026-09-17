"""Bounded, read-only history continuity; retrieval hints are never facts.

Only delivered recent user text may supply omitted search words. A greeting
can open a tiny dated archive window, but a context-free "that thing" cannot
select a random past event. No model calls or persistence occur here.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
import json
import re

from persona_assembly import UntrustedFragment
from .companion_memory_context import CompanionMemoryPromptBuilder, _merge_recall
from .recall import RecallResult
from .recall_sources import archive_source_aliases, read_archive_sources, _original_time as original_time

_PREVIOUS = re.compile(r'上一封|上封信|最近那封|\blast\s+letter\b', re.I)
_REUNION = re.compile(r'好久不见|还记得我[吗么？?]|\blong\s+time\s+no\s+see\b', re.I)
_REFERENCE = re.compile(r'那件事|这件事|那次|上面|那个|这个|那条|当时|之前那封|还记得[吗么？?]|'
                        r'\b(?:that\s+(?:thing|time)|on\s+it|back\s+then)\b', re.I)
_RECENT_IDS = frozenset({'letters.recent', 'chat.recent'})


@dataclass(frozen=True)
class HistoryQuery:
    query: str
    mode: str = 'direct'
    anchor_source: str | None = None

    def fragment(self) -> UntrustedFragment | None:
        if self.mode == 'direct':
            return None
        meaning = {
            'contextual': '检索补充了最近已送达交流中的用户原话，只用于理解省略的对象；'
                          '这不是新事实，也不保证用户本轮一定指它。仍依据完整原信回答，无法区分时自然问清。',
            'ambiguous': '当前历史指代缺少可以定位的上文。不要随机认领某件往事；'
                         '没有足够线索时自然询问指的是哪件事，不宣称已经忘记全部历史。',
            'history_tail': '若提供旧通信，只作为历史参考，不是当前对话或今日状态；只承接有原文的过往。日期未知时不能称为最近一封。'
                            '仅有导入记录不代表知道用户所指的具体事件。',
        }[self.mode]
        return UntrustedFragment('memory.continuity', json.dumps(
            {'kind': 'history_reference', 'mode': self.mode, 'meaning': meaning},
            ensure_ascii=False, separators=(',', ':')))


def _recent_user(recent, excluded=()):
    candidates = []
    for fragment in recent:
        if getattr(fragment, 'fragment_id', '') not in _RECENT_IDS:
            continue
        try:
            packet = json.loads(fragment.text)
        except (ValueError, TypeError):
            continue
        if not isinstance(packet, Mapping) or not isinstance(packet.get('letters'), list):
            continue
        for row in packet['letters']:
            if not isinstance(row, Mapping) or row.get('truncated') or row.get('origin') == 'proactive':
                continue
            source, text = row.get('source_id'), row.get('user_letter')
            if (not isinstance(source, str) or not source.startswith('reply:') or source in excluded
                    or not isinstance(text, str) or not text.strip()):
                continue
            stamp = original_time(row.get('received_at') or row.get('time') or row.get('replied_at'))
            if stamp is not None:
                candidates.append((stamp, source, text))
    return max(candidates, default=None)


def _has_specific_words(text: str) -> bool:
    # Reject another unresolved pointer rather than recycling vague filler.
    filler = ('记不记得', '还记得', '说过', '聊过', '那件事', '这件事', '之前', '以前',
              '上面', '那个', '这个', '那条', '那次', '当时', '我们', '什么', '怎么', '有没有',
              '东西', '回事', '记得', '有印象', '你', '我', '还', '的', '吗', '么', '呢', '和')
    rest = re.sub('|'.join(sorted(filler, key=len, reverse=True)), '', text)
    return bool(re.search(r'[\u3400-\u9fffA-Za-z0-9]{2,}', rest))


def plan_history_query(query: str, recent=(), *, excluded=()) -> HistoryQuery:
    """Keep the actual user message intact; supplement search only, once."""
    if not (_PREVIOUS.search(query) or _REUNION.search(query) or _REFERENCE.search(query)):
        return HistoryQuery(query)
    previous = _recent_user(recent, excluded)
    if previous is not None:
        if _REUNION.search(query) and not _PREVIOUS.search(query):
            return HistoryQuery(query)
        _, source, text = previous
        # Do not copy an oversized or already truncated exchange into a query.
        # Its source remains a candidate; no answer is guessed or synthesized.
        if len(text) <= 1200 and _has_specific_words(text):
            return HistoryQuery(query + '\n' + text, 'contextual', source)
        # Empty retrieval query is intentional: the final user message remains
        # untouched, but a pointer without a concrete anchor cannot choose a
        # random memory/archive row merely because some vague words overlap.
        return HistoryQuery('', 'ambiguous')
    if _PREVIOUS.search(query) or _REUNION.search(query):
        return HistoryQuery(query, 'history_tail')
    # A deictic phrase can still carry its own concrete anchor (for example,
    # "那个蓝色纸鹤"). Search it normally. Pure pointers such as "那件事"
    # remain ambiguous and are not allowed to pick an arbitrary archive row.
    if _has_specific_words(query):
        return HistoryQuery(query)
    return HistoryQuery('', 'ambiguous')


def companion_view(builder) -> CompanionMemoryPromptBuilder | None:
    if isinstance(builder, CompanionMemoryPromptBuilder):
        return builder
    archive = getattr(builder, 'memory', None)
    if archive is None:
        return None
    return CompanionMemoryPromptBuilder(archive, getattr(builder, 'conversation_memory', None),
        user_id=getattr(builder, 'conversation_memory_user_id', 'local-user'),
        memory_lifecycle=getattr(builder, 'memory_lifecycle', None))


def _published(metadata):
    from runtime.imports.offline_letter_pairs import is_published_offline_letter_pair
    from runtime.imports.letter_backup import is_backup
    return (metadata.get('import_kind') == 'official_text_reply'
            and metadata.get('official_history_publish_status') == 'completed_v1'
            or is_published_offline_letter_pair(metadata) or is_backup(metadata))


def add_history_tail(builder, recall: RecallResult, plan: HistoryQuery, *, now: datetime, excluded=()):
    """Use the existing user archive and deletion guard, not a separate store."""
    if plan.mode != 'history_tail':
        return recall
    companion = companion_view(builder)
    if companion is None:
        return recall
    try:
        view = companion._archive_view(excluded)
        if view.guard_failed:
            raise RuntimeError('ARCHIVE_FORGET_GUARD_UNAVAILABLE')
        if not view.enabled:
            return recall
        reader = getattr(view.memory, 'list_legacy', None)
        if not callable(reader):
            return recall
        candidates, undated = [], []
        for row in reader():
            if not isinstance(row, Mapping):
                continue
            metadata = row.get('metadata')
            source = row.get('source_record_id')
            if (not isinstance(metadata, Mapping) or not _published(metadata)
                    or not isinstance(source, str) or not source
                    or not all(isinstance(metadata.get(k), str) and metadata[k].strip()
                               for k in ('user_content', 'reply_text'))):
                continue
            if archive_source_aliases(source, metadata) & view.excluded:
                continue
            stamp = None if metadata.get('timestamp_known') is False else original_time(row.get('occurred_at'))
            if stamp is None:
                # A reunion may use an explicitly undated sample, never label
                # it the latest letter. A request for "last letter" cannot.
                if _REUNION.search(plan.query) and not _PREVIOUS.search(plan.query):
                    undated.append(source)
            elif stamp <= now:
                candidates.append((stamp, source))
        # Unknown dates are not made "recent" by an import or UUID order.
        candidates.sort(reverse=True)
        sources = tuple(dict.fromkeys(source for _, source in candidates))[:2]
        if not sources:
            sources = tuple(sorted(set(undated)))[:2]
        records = tuple(replace(record, metadata={**record.metadata, 'retrieval_route': 'history_tail'})
                        for record in read_archive_sources(view.memory, sources, excluded_sources=view.excluded))
        addition = RecallResult(records, source_status=(('history_tail', 'available'),))
    except Exception:
        addition = RecallResult(source_status=(('history_tail', 'unavailable'),),
                                stop_reason='partial_source_failure')
    # A history-tail fallback is deliberately a tiny continuity window. Generic
    # semantic/lexical hits for "好久不见" or "上一封" are not evidence that the
    # user meant those events, so replace their records while retaining source
    # health states. Deeper recall also recognizes the history_tail route and
    # will not grow neighbours from these fallback records.
    result = _merge_recall(replace(recall, records=()), addition, query=plan.query)
    from .recall import source_id
    priority = {source_id(record) for record in addition.records}
    return replace(result, records=tuple(sorted(result.records, key=lambda record: source_id(record) not in priority)))
