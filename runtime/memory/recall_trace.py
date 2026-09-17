"""Bounded local source reads; finding more context never confirms a fact."""
from dataclasses import replace
import json
import re

from .recall import RecallResult, query_topics, source_id


_RECALL_CUE = re.compile(r'记得|记不记得|回忆|记错|更正|说过|答应|承诺|约定|第一次|\b(?:remember|recall)\b', re.I)


def deepen_recall(builder, recall: RecallResult, *, query, source_ids=(), exclude_source_ids=()) -> RecallResult:
    excluded = tuple(dict.fromkeys(exclude_source_ids))

    def allowed(source):
        return isinstance(source, str) and bool(source) and not source.startswith('day:') and source not in excluded

    def record_allowed(record):
        if not allowed(source_id(record)):
            return False
        try:
            aliases = json.loads(record.metadata.get('source_aliases', '[]'))
        except (TypeError, ValueError):
            return False
        return isinstance(aliases, list) and all(allowed(alias) for alias in aliases)

    records = tuple({r.memory_id: r for r in recall.records if record_allowed(r)}.values())
    result = replace(recall, records=records, topics=recall.topics or query_topics(query))
    # The bounded tail route is already the deliberately tiny continuity fallback
    # for reunion/last-letter prompts. It must never become a seed for neighbour
    # expansion, otherwise two fallback exchanges can silently grow into a
    # much larger unrelated archive window.
    if result.records and all(
        record.metadata.get('retrieval_route') == 'history_tail'
        for record in result.records
    ):
        return replace(result, stop_reason='history_tail_bounded')
    explicit = tuple(dict.fromkeys(source for source in source_ids if allowed(source)))
    seeds = explicit or tuple(result.groups())
    wants_recall = bool(_RECALL_CUE.search(query))
    uncovered = any(item['state'] in {'not_found', 'unavailable', 'partial', 'omitted'}
                    for item in result.coverage(result.records))
    if not explicit and not wants_recall and not uncovered:
        return result
    if not seeds:
        return replace(result, stop_reason='no_source')
    if result.rounds >= 2:
        return replace(result, stop_reason='depth_limit')
    expand = not bool(explicit)
    states = dict(result.source_status)
    seen = {r.memory_id for r in result.records}
    while result.rounds < 2:
        result = replace(result, rounds=result.rounds + 1)
        try:
            structured = getattr(builder, 'trace_sources_result', None)
            if callable(structured):
                traced = structured(seeds, exclude_source_ids=excluded, expand=expand, limit=12)
                found = traced.records
                for channel, state in traced.source_status:
                    # Each round can read different sources; a later success
                    # cannot repair an earlier missing source implicitly.
                    if states.get(channel) not in {'unavailable', 'degraded', 'incomplete'}:
                        states[channel] = state
                if traced.status in {'disabled', 'unavailable'} and not found:
                    states['trace'] = traced.status
                    return replace(result, source_status=tuple(states.items()),
                                   stop_reason='trace_' + traced.status)
            else:
                found = builder.trace_sources(seeds, exclude_source_ids=excluded,
                                              expand=expand, limit=12)
        except Exception as error:
            disabled = isinstance(error, RuntimeError) and str(error) == 'MEMORY_TRACE_DISABLED'
            states['trace'] = 'disabled' if disabled else 'unavailable'
            return replace(result, source_status=tuple(states.items()),
                           stop_reason='trace_disabled' if disabled else 'trace_unavailable')
        additions, resolved = [], []
        for record in found:
            source = source_id(record)
            requested = record.metadata.get('requested_source_id')
            if record_allowed(record) and (expand or source in seeds or requested in seeds):
                if source not in resolved:
                    resolved.append(source)
                if record.memory_id not in seen:
                    additions.append(record)
                    seen.add(record.memory_id)
        states['trace'] = 'available'
        combined = (*result.records, *additions)
        if not expand:
            # A resolved event pointer must not sit behind generic search hits
            # and disappear again when the frozen evidence is packed.
            priority = {source_id(record) for record in combined
                        if source_id(record) in seeds or record.metadata.get('requested_source_id') in seeds}
            combined = tuple(sorted(combined, key=lambda record: source_id(record) not in priority))
        result = replace(result, records=combined, source_status=tuple(states.items()))
        if not additions:
            # Exact reads validate an event pointer even if its original was
            # already retrieved; later corrections still need the neighbor pass.
            if not expand and resolved and (wants_recall or uncovered):
                seeds, expand = tuple(resolved), True
                continue
            return replace(result, stop_reason='no_new_evidence')
        uncovered = any(item['state'] in {'not_found', 'unavailable', 'partial', 'omitted'}
                        for item in result.coverage(result.records))
        if not wants_recall and not uncovered:
            return replace(result, stop_reason='evidence_found')
        seeds = tuple(dict.fromkeys(source_id(record) for record in additions))
        expand = True
    return replace(result, stop_reason='depth_limit')
