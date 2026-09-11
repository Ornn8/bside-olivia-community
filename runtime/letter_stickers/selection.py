"""Reply-call metadata: relation eligibility, compact instruction, strict extraction."""
import json
import re
from functools import lru_cache
from pathlib import Path

BASE = frozenset((1,3,4,6,7,8,9,10,11,12,13,14,15,20,22,23,24,35,41,42,45,47,48,49,50,51,52,53,54))
FAMILIAR = frozenset((2,16,17,18,21,25,26,29,30,32,36,37,38,39,43,46))


def allowed_stickers(view):
    value=lambda key:getattr(getattr(view,key,None),'value',getattr(view,key,None))
    familiar=value('familiarity') in {'medium','high'} or value('relationship_stage') in {'familiar','close','committed'}
    ids=set(BASE)
    if familiar:
        ids.update(FAMILIAR)
        if value('trust') in {'medium','high'} and value('comfort') in {'medium','high'}:
            ids.update(range(1,55))
    return tuple(f'linli-{i:02d}' for i in sorted(ids))


@lru_cache(maxsize=1)
def _labels():
    return {item['id']:item['label'] for item in json.loads(Path(__file__).with_name('catalog.json').read_text(encoding='utf-8'))}


def selection_instruction(allowed):
    return ('呈现元数据约定：先照常写完整回信，末尾另起一行输出 [[sticker:编号]]。'
            '依据整封信的主旨、情绪和当前交流语境，从下列可用插画选一张；不是关键词命中，'
            '严肃、伤心内容不要选搞怪款。可用不等于必须卖萌，也不代表关系身份变化。'
            '该行由程序移除，不解释选择，不写入正文。可选：'+
            '；'.join(f'{i}={_labels()[i]}' for i in allowed))


def split_selection(text, allowed):
    # Strip even a malformed or truncated reserved footer; no corrective LLM call.
    markers=list(re.finditer(r'\[{1,2}\s*sticker\s*:',text,re.IGNORECASE))
    if not markers:
        return text,'linli-01'
    marker=markers[0]
    footer=text[marker.start():].strip()
    match=re.fullmatch(r'\[\[sticker:(linli-\d{2})\]\]',footer)
    chosen=match.group(1) if match and match.group(1) in allowed else 'linli-01'
    return text[:marker.start()].rstrip(),chosen
