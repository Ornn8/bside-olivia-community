# LEGACY memory helper: in-process context only unless persistence is explicitly enabled.
import json, os, time, re

class Memory:
    legacy = True

    def __init__(self, path=None, persist=False):
        self.path = os.fspath(path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'memory_store.json'))
        self.persist = bool(persist)
        self.data = self._load() if self.persist else self._default_data()

    @staticmethod
    def _default_data():
        return {
            'player_profile': {},
            'facts': [],
            'letters': [],
            'last_updated': 0,
        }

    def _load(self):
        default = self._default_data()
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(json.dumps({
                    'event': 'legacy_memory_load_error',
                    'error_type': type(e).__name__,
                }, ensure_ascii=False, sort_keys=True))
        return default

    def save(self):
        if not self.persist:
            return
        self.data['last_updated'] = int(time.time())
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def clear(self):
        """Clear in-memory context and, only when explicitly persistent, its file."""
        self.data = self._default_data()
        if self.persist and os.path.exists(self.path):
            os.remove(self.path)

    # ---- 写入 ----
    def add_letter(self, letter_id, content, reply):
        self.data['letters'].append({
            'id': letter_id, 'content': content, 'reply': reply, 'ts': int(time.time()),
        })
        if len(self.data['letters']) > 100:   # 保留最近 100 封
            self.data['letters'] = self.data['letters'][-100:]
        self.save()

    def update_profile(self, profile: dict):
        self.data['player_profile'].update({k: v for k, v in profile.items() if v})
        self.save()

    def add_fact(self, fact, topic='general'):
        if not fact:
            return
        self.data['facts'].append({'fact': fact, 'topic': topic, 'ts': int(time.time())})
        if len(self.data['facts']) > 60:
            self.data['facts'] = self.data['facts'][-60:]
        self.save()

    # ---- 检索 ----
    def build_context(self, current_content, max_letters=3, max_facts=5):
        """组装记忆上下文注入 prompt"""
        parts = []
        p = self.data['player_profile']
        if p:
            profile_lines = []
            for k, v in p.items():
                profile_lines.append(f'{k}: {v}')
            parts.append('【你对玩家的了解】' + '；'.join(profile_lines))
        if self.data['facts']:
            recent = self.data['facts'][-max_facts:]
            parts.append('【你记得的事】' + '；'.join(f['fact'] for f in reversed(recent)))
        if self.data['letters']:
            recent_letters = self.data['letters'][-max_letters:]
            lines = []
            for l in recent_letters:
                lines.append(f'玩家说：{l["content"][:120]}')
                if l.get('reply'):
                    lines.append(f'你回：{l["reply"][:120]}')
            parts.append('【最近的通信】' + '\n'.join(lines))
        # 话题相关旧信（简单关键词匹配当前信）
        related = self._find_related(current_content)
        if related:
            parts.append('【相关旧信】' + '；'.join(f'玩家曾提到：{r["content"][:100]}' for r in related))
        return '\n\n'.join(parts)

    def _find_related(self, content, limit=2):
        # 关键词抽取（简单分词：取 2 字以上中文词 + 英文词）
        words = set(re.findall(r'[\u4e00-\u9fff]{2,4}|[a-zA-Z]{3,}', content))
        if not words:
            return []
        scored = []
        for l in self.data['letters']:
            c = l['content']
            score = sum(1 for w in words if w in c)
            if score > 0:
                scored.append((score, l))
        scored.sort(key=lambda x: -x[0])
        return [l for _, l in scored[:limit]]
