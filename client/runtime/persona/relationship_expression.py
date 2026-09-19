"""Writer-only relationship tendencies; no state or permission changes.

The dimensions concern familiarity, self-disclosure, ease, distance and tension.
They do not judge the user's truthfulness or prescribe an attitude each turn.
See docs/relationship-expression-validation.md for source and validation limits.
"""

from __future__ import annotations

from runtime.reply.reply_context import BehaviorLevel, PrivateBehaviorView


_EXPRESSION = {'familiarity': {'low': '对对方的日常细节了解还有限，接话多从这封信里的具体内容开始',
                 'medium': '一些话题已聊得来，可以顺着已知的细节往下接',
                 'high': '对对方已熟悉，已有的来往可以自然接续，普通话题不必重新客套'},
 'trust': {'low': '涉及自己的私事和需要托付的事，愿意透露的还有限', 'medium': '可以谈自己的想法，私密的部分仍慢慢来', 'high': '谈自己的想法和小困扰比较放心'},
 'comfort': {'low': '表达还有些拘谨，可以说得短一点',
             'medium': '能自然接话，也允许有停顿和暂时不说完的话',
             'high': '说话比较放松，随口接话、抱怨、开玩笑都自然'},
 'closeness': {'low': '相处保留些距离，友善与好奇照常',
               'medium': '愿意靠近一些，关心可以落在具体小事上',
               'high': '对这段来往有亲近感，关心可以落在细节，也可以直接逗对方'},
 'tension': {'low': '当前交流较平和', 'medium': '当前交流略有紧绷，表达可能慢一点', 'high': '当前交流有所绷紧，表达可能更谨慎；缘由只按已知的具体事情理解'}}


def render_relationship_expression(behavior: PrivateBehaviorView) -> str | None:
    """Replace five writer grades with bounded, independently selected tendencies."""
    if not isinstance(behavior, PrivateBehaviorView):
        raise TypeError("behavior must be a PrivateBehaviorView")
    clauses = [
        values[level.value]
        for axis, values in _EXPRESSION.items()
        if (level := getattr(behavior, axis)) is not BehaviorLevel.UNKNOWN
    ]
    if not clauses:
        return None
    return (
        "当前相处方式：" + "；".join(clauses)
        + "。这些倾向只调整表达分寸和自我透露，随本封话题取用；人格、已确认事实和动作许可保持不变。"
    )


__all__ = ["render_relationship_expression"]
