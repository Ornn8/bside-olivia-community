"""Per-request retrieval evidence and final inclusion, never a fact-confirmation ledger."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re


def query_topics(query: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip() for part in
        re.split(r'[。！？?!，,；;\n\r\u2028\u2029]+', query) if part.strip()))


def source_id(record) -> str:
    return (getattr(record, 'source_id', None)
            or getattr(record, 'provenance', {}).get('source_record_id')
            or record.memory_id)


@dataclass(frozen=True)
class RecallResult:
    records: tuple = ()
    topics: tuple[str, ...] = ()
    source_status: tuple[tuple[str, str], ...] = ()
    rounds: int = 0
    stop_reason: str = 'complete'

    @property
    def status(self) -> str:
        states = [state for _, state in self.source_status]
        if any(state in {'unavailable', 'degraded', 'incomplete'} for state in states):
            return 'degraded' if self.records else 'unavailable'
        if states and all(state in {'disabled', 'paused'} for state in states):
            return 'disabled'
        return 'available'

    def groups(self) -> dict[str, tuple]:
        grouped = {}
        for record in self.records:
            grouped.setdefault(source_id(record), []).append(record)
        return {key: tuple(value) for key, value in grouped.items()}

    def coverage(self, selected) -> tuple[dict, ...]:
        # "evidence_found" means candidates were retrieved, not that a claim is true.
        from .source_retrieval import terms
        selected_ids = {r.memory_id for r in selected}
        failures = any(s in {'unavailable', 'degraded', 'incomplete'} for _, s in self.source_status)
        result = []
        for index, topic in enumerate(self.topics):
            tokens = set(terms(topic))
            matches = []
            for record in self.records:
                declared = str(record.metadata.get('topic_indexes', '')).split(',')
                if str(index) in declared or (not record.metadata.get('topic_indexes')
                        and tokens.intersection(terms(record.text))):
                    matches.append(record)
            found = {source_id(r) for r in matches}
            # A group's missing answer/correction makes its coverage incomplete.
            retained = {source for source, group in self.groups().items()
                        if all(r.memory_id in selected_ids for r in group)}
            state = ('evidence_found' if found and found <= retained else
                     'partial' if found & retained else 'omitted' if found else
                     'unavailable' if failures else 'not_found')
            result.append({'topic': index, 'state': state, 'sources': tuple(sorted(found & retained))})
        return tuple(result)

    def summary(self, selected) -> dict:
        selected_ids = {r.memory_id for r in selected}
        included, omitted = [], []
        for source, group in self.groups().items():
            (included if all(r.memory_id in selected_ids for r in group) else omitted).append(source)
        return {'source_status': dict(self.source_status), 'rounds': self.rounds,
                'stop_reason': self.stop_reason, 'included_groups': included,
                'omitted_groups': omitted, 'topic_coverage': self.coverage(selected)}

    def state_text(self, selected, *, detailed=True) -> str:
        summary = self.summary(selected)
        if not detailed:
            summary = {'source_status': summary['source_status'], 'stop_reason': self.stop_reason,
                       'included': len(summary['included_groups']), 'omitted': len(summary['omitted_groups']),
                       'topic_coverage': [c['state'] for c in summary['topic_coverage']]}
        return json.dumps(summary, ensure_ascii=False, separators=(',', ':'))
