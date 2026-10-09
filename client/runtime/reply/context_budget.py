"""Repack optional evidence locally, keeping source/dependency groups intact.

This is the final character budget shared by decision, delivery and speech
projection. It does not delete stored correspondence, summarize originals or call a
model. Unknown containers, relationship evidence and current input stay fixed.
"""
from dataclasses import dataclass
import json
import re


_BLOCK = re.compile(r'<(untrusted_history|evidence_summary)>\s*(\{.*?\})\s*</\1>', re.S)
_IDENTITIES = {'source', 'source_id', 'source_record_id', 'event_id', 'citation',
               'text_ref', 'source_ref', 'earlier_source', 'later_source', 'earlier', 'later'}
OMISSION_NOTE = '部分历史及其关联原话因本轮容量一起省略；缺失不表示没有发生，不能据此否认旧约定或补写细节。'


def _identities(value):
    if isinstance(value, list):
        return set().union(*(_identities(v) for v in value)) if value else set()
    if not isinstance(value, dict):
        return set()
    result = {v for k, v in value.items() if k in _IDENTITIES and isinstance(v, str) and v}
    for v in value.values():
        if isinstance(v, (dict, list)):
            result.update(_identities(v))
    return result


def _encode(tag, wrapper):
    return '<' + tag + '>' + json.dumps(wrapper, ensure_ascii=False, separators=(',', ':')).replace(
        '<', r'\u003c').replace('>', r'\u003e') + '</' + tag + '>'


@dataclass
class _Unit:
    identities: set
    position: int
    native: bool = False


def fit_reply_context(messages, *, max_input_chars):
    """One bounded local packing pass; references and their originals move together."""
    if sum(len(m['content']) for m in messages) <= max_input_chars:
        return tuple(messages)
    units, renderers, protected = [], [], []
    current = next((i for i in range(len(messages)-1, -1, -1)
                    if messages[i].get('role') == 'user'), len(messages))

    def unit(value, position, native=False):
        key = len(units)
        units.append(_Unit(_identities(value), position, native))
        return key

    def block(match, position):
        original, tag = match.group(0), match.group(1)
        try:
            from runtime.memory.memory_prompt import _unescape_reserved
            wrapper = json.loads(match.group(2))
            text = _unescape_reserved(wrapper['text'])
            if tag == 'untrusted_history' and text.startswith('[ORIGINAL_CORRESPONDENCE_UNTRUSTED]\n'):
                # A selected recall envelope already closes over corrections.
                # Keep the whole envelope atomic, including cross-group refs.
                groups = [json.loads(line) for line in text.split('\n') if line.startswith('[{')]
                if groups:
                    key = unit(groups, position)
                    return lambda removed: '' if key in removed else original
            packet = json.loads(text)
            if tag == 'untrusted_history' and isinstance(packet, dict) and packet.get('kind') in {
                    'delivered_media', 'recent_dialogue'}:
                key = unit(packet, position)
                return lambda removed: '' if key in removed else original
            if (tag == 'evidence_summary' and wrapper.get('fragment_id') == 'linli.daily-life'
                    and isinstance(packet, dict) and isinstance(packet.get('previous_observations'), list)):
                observations = packet['previous_observations']
                keys = [unit(item, position) for item in observations]
                base = {**packet, 'previous_observations': []}
                protected.append(_encode(tag, {**wrapper, 'text': json.dumps(base, ensure_ascii=False)}))

                def render(removed):
                    if not any(k in removed for k in keys):
                        return original
                    kept = [item for k, item in zip(keys, observations) if k not in removed]
                    value = {**packet, 'previous_observations': kept}
                    return _encode(tag, {**wrapper, 'text': json.dumps(value, ensure_ascii=False, separators=(',', ':'))})
                return render
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
        # Includes relationship records, current input evidence and unknown
        # formats: a capacity repair must not weaken their meaning.
        protected.append(original)
        return lambda removed: original

    for position, message in enumerate(messages):
        content = message['content']
        if position < current and message.get('role') in {'user', 'assistant'} and content.startswith('[历史消息 '):
            header, separator, _ = content.partition(']\n')
            try:
                meta = json.loads(header[len('[历史消息 '):])
                if not separator or not isinstance(meta, dict):
                    raise ValueError
                key = unit(meta, position, native=True)
                renderers.append(lambda removed, k=key, c=content: '' if k in removed else c)
                continue
            except (ValueError, TypeError):
                pass
        if message.get('role') != 'system':
            renderers.append(lambda removed, c=content: c)
            continue
        parts, start = [], 0
        for match in _BLOCK.finditer(content):
            plain = content[start:match.start()]
            protected.append(plain)
            parts.append(lambda removed, c=plain: c)
            parts.append(block(match, position))
            start = match.end()
        tail = content[start:]
        protected.append(tail)
        parts.append(lambda removed, c=tail: c)
        renderers.append(lambda removed, parts=parts: ''.join(part(removed) for part in parts))

    # Same exchange, compacted references and known correction edges form one
    # component. Fixed evidence pins the entire connected component.
    parents = list(range(len(units)))
    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index
    owners = {}
    for index, item in enumerate(units):
        for identity in item.identities:
            if identity in owners:
                parents[root(index)] = root(owners[identity])
            owners[identity] = index
    groups = {}
    for index in range(len(units)):
        groups.setdefault(root(index), []).append(index)
    fixed = '\n'.join(protected)
    pinned = {root(i) for i, item in enumerate(units) if any(identity in fixed for identity in item.identities)}
    latest = max((u.position for u in units if u.native), default=-1)
    def priority(group):
        native = [units[i].position for i in group if units[i].native]
        return (2 if latest in native else 1 if any(not units[i].native for i in group) else 0,
                min(native, default=min(units[i].position for i in group)))
    choices = sorted((g for k, g in groups.items() if k not in pinned), key=priority)
    removed = set()
    for group in choices:
        removed.update(group)
        result = [{**m, 'content': render(removed)} for m, render in zip(messages, renderers)]
        result = [m for m in result if m['content']]
        if (any(not units[i].native for i in removed)
                and not any(m.get('role') == 'system' and m['content'] == OMISSION_NOTE for m in result)):
            target = next((i for i in range(len(result)-1, -1, -1)
                           if result[i].get('role') == 'user'), len(result))
            # Keep the final output/speech contract adjacent to current input.
            if target and result[target-1].get('role') == 'system':
                target -= 1
            result.insert(target, {'role': 'system', 'content': OMISSION_NOTE})
        if sum(len(m['content']) for m in result) <= max_input_chars:
            return tuple(result)
    raise ValueError('INPUT_TOO_LONG')
