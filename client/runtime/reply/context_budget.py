"""Repack optional evidence locally, keeping source/dependency groups intact.

This is the final character budget shared by decision, delivery and speech
projection. It does not delete stored correspondence, summarize originals or call a
model. Core contracts and current input stay fixed; other containers are optional.
"""
from dataclasses import dataclass
import json
import re


_BLOCK = re.compile(r'<([a-z][a-z0-9_]*)>\s*(.*?)\s*</\1>', re.S)
_IDENTITIES = {'source', 'source_id', 'source_record_id', 'event_id', 'citation',
               'text_ref', 'source_ref', 'earlier_source', 'later_source', 'earlier', 'later',
               'source_ids', 'evidence_turn_ids'}
# QQ writer requests: about 0.26-0.37 tokens per serialized byte for Chinese JSON
# (measured on live requests), so 250 KB keeps every chat model under 100k tokens.
# Packing stops a little earlier so the final contract always fits under the hard cap.
PERSONAL_CHAT_MAX_INPUT_BYTES = 250_000
PERSONAL_CHAT_FIT_BYTES = 245_000
OMISSION_NOTE = '部分历史及其关联原话因本轮容量一起省略；缺失不表示没有发生，不能据此否认旧约定或补写细节。'


def _identities(value):
    if isinstance(value, list):
        return set().union(*(_identities(v) for v in value)) if value else set()
    if not isinstance(value, dict):
        return set()
    result = set()
    for key, item in value.items():
        if key in _IDENTITIES:
            result.update(v for v in (item if isinstance(item, list) else [item]) if isinstance(v, str) and v)
    for v in value.values():
        if isinstance(v, (dict, list)):
            result.update(_identities(v))
    return result



def _references(text):
    result = set()
    decoder = json.JSONDecoder()
    at = 0
    while at < len(text):
        start = text.find('{', at)
        if start < 0:
            break
        try:
            value, used = decoder.raw_decode(text[start:])
        except ValueError:
            at = start + 1
        else:
            result.update(_identities(value))
            if isinstance(value, dict) and set(value) <= {'fragment_id', 'untrusted', 'text'}:
                try:
                    packet = json.loads(value.get('text', ''))
                except (ValueError, TypeError):
                    pass
                else:
                    result.update(_identities(packet))
            at = start + used
    return result


_FIXED_TAGS = frozenset({
    'constitution', 'forbidden', 'persona_profile', 'mode_constraints', 'mode_style',
    'character_participation', 'runtime_time', 'chat_delivery',
    'evidence_use', 'grounding', 'relationship_grounding', 'companion_decision',
    'chat_output_rules', 'speech_request', 'bedtime_audio_offer', 'daily_video_candidates',
    'reply_delivery_plan',
})


def wire_size(messages):
    return len(json.dumps(list(messages), ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


class ContextCapacityError(ValueError):
    def __init__(self, report):
        super().__init__('INPUT_TOO_LONG')
        self.failure_context = report


def _encode(tag, wrapper):
    return '<' + tag + '>' + json.dumps(wrapper, ensure_ascii=False, separators=(',', ':')).replace(
        '<', r'\u003c').replace('>', r'\u003e') + '</' + tag + '>'


@dataclass
class _Unit:
    identities: set
    position: int
    native: bool = False
    keep_last: bool = False


def fit_reply_context(messages, *, max_input_chars, max_input_bytes=None):
    """One bounded local packing pass; references and their originals move together."""
    def fits(value):
        return (sum(len(m['content']) for m in value) <= max_input_chars
                and (max_input_bytes is None or wire_size(value) <= max_input_bytes))
    if fits(messages):
        return tuple(messages)
    units, renderers, protected = [], [], []
    current = next((i for i in range(len(messages)-1, -1, -1)
                    if messages[i].get('role') == 'user'), len(messages))

    def unit(value, position, native=False, keep_last=False):
        key = len(units)
        units.append(_Unit(_identities(value), position, native, keep_last))
        return key

    def block(match, position):
        original, tag = match.group(0), match.group(1)
        try:
            from runtime.memory.memory_prompt import _unescape_reserved
            if tag not in {'untrusted_history', 'evidence_summary'}:
                if tag in _FIXED_TAGS:
                    protected.append(original)
                    return lambda removed: original
                key = unit({'source_ids': sorted(_references(match.group(2)))}, position)
                return lambda removed: '' if key in removed else original
            wrapper = json.loads(match.group(2))
            text = _unescape_reserved(wrapper['text'])
            if tag == 'untrusted_history' and text.startswith('[ORIGINAL_CORRESPONDENCE_UNTRUSTED]\n'):
                lines = text.split('\n')
                keys = {i: unit(json.loads(line), position) for i, line in enumerate(lines)
                        if line.startswith('[{')}
                if keys:
                    def render(removed):
                        if not any(k in removed for k in keys.values()):
                            return original
                        kept = [line for i, line in enumerate(lines) if i not in keys or keys[i] not in removed]
                        if all(k in removed for k in keys.values()):
                            return ''
                        return _encode(tag, {**wrapper, 'text': '\n'.join(kept)})
                    return render
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
                base_key = unit(base, position, keep_last=True)

                def render(removed):
                    if base_key in removed:
                        return ''
                    if not any(k in removed for k in keys):
                        return original
                    kept = [item for k, item in zip(keys, observations) if k not in removed]
                    value = {**packet, 'previous_observations': kept}
                    return _encode(tag, {**wrapper, 'text': json.dumps(value, ensure_ascii=False, separators=(',', ':'))})
                return render
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
        fixed = (tag in _FIXED_TAGS or tag == 'evidence_summary' and
                 isinstance(locals().get('wrapper'), dict) and
                 wrapper.get('fragment_id') in {'current.input_evidence', 'speech.continuation'})
        packet = locals().get('packet')
        fixed = fixed or isinstance(packet, dict) and packet.get('kind') in {
            'relationship_history', 'relationship_diary'}
        if fixed:
            protected.append(original)
            return lambda removed: original
        key = unit({'source_ids': sorted(_references(original))}, position)
        return lambda removed: '' if key in removed else original

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
    fixed = set().union(*(_references(part) for part in protected)) if protected else set()
    pinned = {root(i) for i, item in enumerate(units) if item.identities & fixed}
    latest = max((u.position for u in units if u.native), default=-1)
    def priority(group):
        native = [units[i].position for i in group if units[i].native]
        return (3 if any(units[i].keep_last for i in group) else
                2 if latest in native else 1 if any(not units[i].native for i in group) else 0,
                min(native, default=min(units[i].position for i in group)))
    choices = sorted((g for k, g in groups.items() if k not in pinned), key=priority)
    def pack(count):
        removed = {i for group in choices[:count] for i in group}
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
        return result, removed
    result, removed = pack(len(choices))
    if fits(result):
        low, high = 0, len(choices)
        while low < high:
            middle = (low + high) // 2
            candidate, _ = pack(middle)
            if fits(candidate):
                high = middle
            else:
                low = middle + 1
        return tuple(pack(low)[0])
    fixed_result = [dict(m, content=render(removed)) for m, render in zip(messages, renderers)]
    fixed_chars = sum(len(m['content']) for m in fixed_result)
    costs = dict(dialogue_chars=0, recall_chars=0, world_chars=0, emotion_chars=0,
                 contract_chars=0, core_chars=0, other_chars=0)
    for position, message in enumerate(messages):
        content = message['content']
        if position == current:
            continue
        if message.get('role') != 'system':
            costs['dialogue_chars'] += len(content)
            continue
        end = 0
        for match in _BLOCK.finditer(content):
            costs['core_chars'] += match.start() - end
            tag, raw = match.group(1), match.group(0)
            category = ('recall_chars' if tag == 'untrusted_history' else
                        'world_chars' if tag == 'evidence_summary' else
                        'emotion_chars' if tag == 'character_emotion' else
                        'contract_chars' if tag in {'companion_decision', 'chat_output_rules',
                            'speech_request', 'bedtime_audio_offer', 'reply_delivery_plan'} else
                        'core_chars' if tag in _FIXED_TAGS else 'other_chars')
            costs[category] += len(raw)
            end = match.end()
        costs['core_chars'] += len(content) - end
    raise ContextCapacityError(dict(failure_stage='writer_context', failure_detail='final_rules_budget',
        input_chars=sum(len(m['content']) for m in messages), max_input_chars=max(0, max_input_chars),
        input_bytes=wire_size(messages), max_input_bytes=max_input_bytes or 0,
        fixed_chars=fixed_chars, optional_chars=max(0, sum(len(m['content']) for m in messages)-fixed_chars),
        current_input_chars=len(messages[current]['content']) if current < len(messages) else 0,
        packed_chars=sum(len(m['content']) for m in result), **costs))
