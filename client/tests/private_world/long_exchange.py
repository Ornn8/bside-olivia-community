"""Deterministic long-history exchange fixture (realistic Chinese, no network, no paid call).

Shared by the bounded-request tests and by the before/after measurement script, so both
describe the same user: a long history of projects (most of them still open), a long
boundary list, and a long letter and reply made of many comma clauses.
"""
import hashlib
import random
from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 10, 11, 4, 0, tzinfo=timezone.utc)

_WHEN = ['早上', '上午', '中午', '下午', '傍晚', '晚上', '周末', '前天', '昨天', '这几天', '刚才', '这周', '前些天']
_ACTS = [
    '去楼下的早餐店买了豆浆和包子', '把窗台上的绿萝换了新的花盆', '给老家的妈妈打了一通很长的电话', '练了四十分钟的钢琴',
    '在图书馆借了两本关于植物的书', '把房间里堆了很久的旧杂志整理好了', '骑车绕着湖边转了一圈', '煮了一锅番茄牛腩面',
    '重新读了一遍那本没读完的小说', '给同事带了一份热腾腾的早餐', '洗了两大盆衣服并且晾在阳台上', '陪邻居家的小孩做了半天作业',
    '去超市买了一些新鲜的蔬菜和水果', '把手机里的照片分门别类地备份了', '听了一整张很安静的专辑', '写了一页毛笔字',
    '在公园的长椅上坐着晒了会儿太阳', '修好了那把一直松动的椅子', '把下个月的开销大致算了一遍', '试着做了一次手冲咖啡',
    '整理了书架上最乱的那一层', '给朋友寄去了一张手写的明信片', '看了一部很老的电影', '把阳台上的小番茄浇了水',
    '去菜市场挑了一条很新鲜的鱼', '给自己泡了一壶桂花乌龙', '学着折了几只纸鹤', '把旧毛衣拆了准备重新织',
    '沿着河边慢慢走了半个小时', '在笔记本上列好了接下来一周的安排', '把厨房的抽油烟机仔细擦了一遍', '去理发店剪短了头发',
    '试听了几首适合睡前的轻音乐', '帮楼下的奶奶搬了一袋米', '给阳台的花换了土', '给自己拍了几张照片留念',
]
_REACTIONS = [
    '心里一下子踏实了不少', '发现比想象中容易很多', '虽然有点累但是特别开心', '一直在想你上次说过的话',
    '不知道你会不会觉得有点无聊', '其实还是有一点紧张', '感觉这几天过得比平时都慢', '真的很想把这些都讲给你听',
    '没有像之前那样焦虑了', '只是不太确定自己做得对不对', '总觉得还差一点点什么', '心情比前几天好了许多',
]
_CONDITIONS = [
    ('如果明天不下雨', '我就去公园慢跑'), ('要是周六有空', '我想把这首曲子完整录下来'),
    ('等考试结束以后', '我会好好睡上一整天'), ('如果你方便的话', '我可以把照片整理好发给你'),
    ('要是你愿意', '下次我们可以一起去看展'), ('等这几天忙完', '我准备给你寄一封长一点的信'),
    ('如果天气继续转凉', '我打算把厚外套拿出来晒一晒'), ('要是周日晚上没有安排', '我想再把那本书读完一章'),
]
_CONNECT = ['然后', '也', '还', '并且', '同时', '而且', '又']
_OBJECTS = ['那首曲子', '周末的计划', '给妈妈的电话', '图书馆的书', '阳台的花', '房间的整理', '新买的咖啡豆', '手写的明信片',
            '下个月的预算', '那部老电影', '早晨的慢跑', '厨房的大扫除']


def _clause(rng):
    return rng.choice(_WHEN) + rng.choice(_ACTS)


def sentence(rng, clauses):
    parts = [_clause(rng)]
    while len(parts) < clauses:
        roll = rng.random()
        if roll < .45:
            parts.append(rng.choice(_CONNECT) + rng.choice(_ACTS))
        elif roll < .75:
            parts.append(rng.choice(_REACTIONS))
        else:
            condition, promise = rng.choice(_CONDITIONS)
            parts.extend([condition, promise])
    return '，'.join(parts[:max(clauses, 2)]) + '。'


def text(seed, chars, *, clauses=(4, 8), opener=''):
    """Letter-like Chinese text of about ``chars`` characters made of comma-heavy sentences."""
    rng = random.Random(seed)
    sentences, size = [], len(opener)
    while size < chars:
        value = sentence(rng, rng.randint(*clauses))
        sentences.append(value)
        size += len(value)
    return opener + ''.join(sentences)


def projects(count=160, open_count=90, *, seed=7, now=NOW, mentioned_titles=()):
    """Items shaped like ``DailyLifeStore.exchange_state`` output: open items carry full evidence,
    finished items are identities. ``mentioned_titles`` become stale open items."""
    rng = random.Random(seed)
    result = {'projects': [], 'shared': []}
    for index in range(count):
        is_open = index < open_count
        stale = index >= open_count // 3 or index in range(len(mentioned_titles))
        title = (mentioned_titles[index] if index < len(mentioned_titles)
                 else rng.choice(_ACTS)[:18] + rng.choice(_OBJECTS))
        age = timedelta(days=rng.randint(20, 120) if stale and is_open else rng.randint(0, 12),
                        hours=rng.randint(0, 23))
        kind = 'shared' if index % 3 == 0 else 'linli'
        item = {'id': 'jev.' + hashlib.sha256(f'fixture{index}'.encode()).hexdigest()[:40],
                'title': title[:60], 'kind': kind,
                'actor': 'linli' if index % 5 else 'user',
                'status': (['planned', 'ongoing', 'paused', 'awaiting_user'][index % 4] if is_open
                           else ['completed', 'cancelled'][index % 2]),
                'source_id': f'reply:fixture{index}', 'updated_at': (now - age).isoformat()}
        if is_open:
            quote = sentence(rng, 3)[:200]
            item.update(detail=quote, quote=quote, evidence_kind='character_statement')
        result['projects' if kind == 'linli' else 'shared'].append(item)
    return result


def boundaries(count=40, *, seed=11, now=NOW):
    rng = random.Random(seed)
    return [{'boundary_id': f'b{i + 1}', 'set_at': (now - timedelta(days=i + 1)).isoformat(),
             'scope': (rng.choice(['夜里十一点以后', '工作日白天', '她在练琴时', '周末上午', '考试前一周']) +
                       '不希望被' + rng.choice(['打电话打扰', '频繁追问', '催着回复', '连续发语音', '要求视频']) +
                       '，除非是特别紧急的事情，她会自己找时间回复。')} for i in range(count)]


def build(*, letter_chars=2400, reply_chars=2000, project_count=160, open_count=90, boundary_count=40,
          contact_invited=False, seed=1):
    """Request data as ``consume_exchange`` hands it to ``jev_exchange.extract``."""
    letter = text(seed, letter_chars, opener='你好呀，最近怎么样？')
    reply = text(seed + 100, reply_chars, opener='收到你的信了，')
    # Three long-stale open items that this exchange actually talks about.
    sentences = [s for s in reply.split('。') if s]
    def pick(parts, index):
        return parts[min(index, len(parts) - 1)]
    titles = [pick(letter.split('，'), 3)[:20], pick(pick(sentences, 2).split('，'), 1)[:20],
              pick(pick(sentences, 6).split('，'), 2)[:20]]
    return {
        'user_letter': letter, 'linli_reply': reply, 'origin': 'user', 'contact_invited': contact_invited,
        'previous_state': projects(project_count, open_count, mentioned_titles=titles),
        'active_boundaries': boundaries(boundary_count),
        'rhythm': {'local_time': '2026-10-11T12:00:00+08:00', 'phase': 'day', 'as_of': NOW.isoformat(),
                   'wellbeing': {'state': 'well', 'care': 'normal', 'evidence': '较长的历史依据' * 40}},
        'previous_observation': {'activity': '练琴', 'location': '家里', 'note': '刚弹完一遍新曲子，手指有点酸。',
                                 'status': 'ongoing', 'actor': 'linli', 'evidence_kind': 'published_life',
                                 'occurred_at': (NOW - timedelta(hours=3)).isoformat()},
        'meals_today': [{'slot': 'breakfast', 'status': 'eaten', 'food': '豆浆和包子'},
                        {'slot': 'lunch', 'status': 'eaten', 'food': '番茄牛腩面'}],
        'development_topics': [], 'character_development': {'stage': 'steady', 'history': '很长的发展记录' * 100},
    }
