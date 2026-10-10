"""Reply-call metadata: relation eligibility, compact instruction, strict extraction."""
import json
import random
import re
from functools import lru_cache
from pathlib import Path

BASE = frozenset((1,3,4,6,7,8,9,10,11,12,13,14,15,20,22,23,24,35,41,42,45,47,48,49,50,51,52,53,54))
FAMILIAR = frozenset((2,16,17,18,21,25,26,29,30,32,36,37,38,39,43,46))
NEW_STICKERS = range(109, 273)
JOJO_STICKERS = frozenset(f'linli-{i:03d}' for i in range(229, 253))


def allowed_stickers(view, *, channel='letter', installed=()):
    # QQ also offers the optional styles, but only those the user installed as packs.
    value=lambda key:getattr(getattr(view,key,None),'value',getattr(view,key,None))
    familiar=value('familiarity') in {'medium','high'} or value('relationship_stage') in {'familiar','close','committed'}
    ids=set(BASE)
    if familiar:
        ids.update(FAMILIAR)
        ids.update(range(55,64))
        if value('trust') in {'medium','high'} and value('comfort') in {'medium','high'}:
            ids.update(range(1,73))
            if all(value(key)=='high' for key in ('closeness','trust','comfort')):
                ids.update(range(73,109))
    if channel == 'qq':
        ids.update(i for i in NEW_STICKERS if f'linli-{i:03d}' in installed)
    return tuple(f'linli-{i:02d}' for i in sorted(ids))


@lru_cache(maxsize=1)
def _catalog():
    return json.loads(Path(__file__).with_name('catalog.json').read_text(encoding='utf-8'))


@lru_cache(maxsize=1)
def _labels():
    return {item['id']: item['label'] for item in _catalog()}


@lru_cache(maxsize=1)
def _files():
    return {item['id']: item['file'] for item in _catalog()}


def asset_filename(sticker_id):
    return _files()[sticker_id]


def weighted_candidates(allowed, history, *, limit, appeared=None, rng=None):
    """Keep the existing recency/count lottery; Linli chooses the final image."""
    available = list(allowed)
    counts = {key: history.count(key) for key in available}
    last = {key: index for index, key in enumerate(history if appeared is None else appeared)
            if key is not None}
    weights = {key: (1 + min(256, len(history) - last.get(key, -1))) / (1 + counts[key])
               * (0.1 if key in JOJO_STICKERS else 1.0) for key in available}
    selected = []
    for _ in range(min(limit, len(available))):
        key = (rng or random).choices(available, weights=[weights[item] for item in available])[0]
        selected.append(key)
        available.remove(key)
    return tuple(selected)


def selection_instruction(allowed):
    return ('呈现元数据约定：先照常写完整回信，末尾另起一行输出 [[sticker:编号]]。'
            '依据整封信的主旨、情绪和当前交流语境，从下列可用插画选一张；不是关键词命中，'
            '严肃、伤心内容不要选搞怪款。可用不等于必须卖萌，也不代表关系身份变化。'
            '奇幻想象仅为插画，不把画面写成真实经历或已发生的亲密行为。'
            '该行由程序移除，不解释选择，不写入正文。可选：'+
            '；'.join(f'{i}={_labels()[i]}' for i in allowed))


# The model sometimes names the reserved line differently ([[image:...]]) or
# puts it first. Any such marker is metadata wherever it appears, never prose.
_MARKER = re.compile(
    r'\[{1,2}\s*(?:sticker|image|img|picture|pic|illustration|插画|插图|表情|贴纸)\s*[:：]'
    r'[^\[\]\n]{0,40}(?:\]{1,2}|$)', re.IGNORECASE | re.MULTILINE)
_WELL_FORMED = re.compile(r'\[\[\s*[^\[\]:：]+\s*[:：]\s*(linli-\d{2,3})\s*\]\]')


def split_selection(text, allowed):
    # Strip every reserved marker, even malformed or truncated; no corrective LLM call.
    markers = list(_MARKER.finditer(text))
    if not markers:
        return text,'linli-01'
    chosen = 'linli-01'
    for marker in markers:
        match = _WELL_FORMED.fullmatch(marker.group(0).strip())
        if match and match.group(1) in allowed:
            chosen = match.group(1)
            break
    cleaned = _MARKER.sub('', text)
    cleaned = re.sub(r'[ \t]+\n', '\n', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned,chosen
