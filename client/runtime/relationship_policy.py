"""Built-in text for the relationship surface."""
ROUTES = {'undecided': '先不定义', 'romantic': '恋人', 'friend': '挚友', 'family': '家人'}
DEFAULTS = {
    'rising': {'familiarity': '最近她更熟悉你了。', 'trust': '最近她更信任你了。',
        'comfort': '最近她在你面前更自在了。', 'closeness': '最近你们更亲近了。', 'tension': '最近你们之间有些紧张。'},
    'route_lines': {
        'undecided': '相处方式先不定义。按真实互动与当前关系阶段自然相处，不假定恋爱关系。',
        'romantic': '用户已在关系页确认恋人关系，聊天里自然接住这份心意，不再要求二次确认。可以直接表达想念，关心落在具体近况，不每次叮嘱。',
        'friend': '以挚友相处。自在、坦诚，有具体困难再关心，不使用恋爱措辞、调情或情话。',
        'family': '以家人般的方式关心彼此，可适时问候生活近况，不连续叮嘱。不假定血缘，不强制称呼，不使用恋爱措辞、调情或情话。',
    },
    'route_ask': '这次聊天自然地问一次：你希望我们以后怎样相处？让对方按自己的心意在关系页选择，不替对方决定。',
    'volumes': {chosen: {
        '1': {'title': '初识', 'description': '你们刚刚走进彼此的生活，正在一点点认识对方。'},
        '2': {'title': '熟悉', 'description': '日常的分享让彼此不再陌生，聊天渐渐变得自在。'},
        '3': {'title': '朋友', 'description': '你们愿意分享生活里的小事，也开始在意彼此的心情。'},
        '4': {'title': '信赖', 'description': '在一次次倾听和回应里，你们有了可以安心依靠的信任。'},
        '5': {'title': {'romantic': '心意相通', 'friend': '无话不说', 'family': '像家人一样', 'undecided': '信赖'}[chosen],
              'description': '你们选择了适合彼此的相处方式，继续在真实的生活里陪伴对方。'},
        '6': {'title': '共同生活', 'description': '新的季节来到，你们继续分享平凡而珍贵的日子。'},
    } for chosen in ROUTES},
    'dimensions': {name: dict(zip(('unknown', 'low', 'medium', 'high'), lines)) for name, lines in {
        'familiarity': ('她还在认识你。', '她正在慢慢了解你的日常。', '她已熟悉不少关于你的事。', '你们对彼此的生活很熟悉。'),
        'trust': ('信任正在萌芽。', '她开始愿意向你敞开心扉。', '她愿意把重要的心事告诉你。', '她很信任你，也珍惜这份信任。'),
        'comfort': ('她还需要一点时间放松。', '和你相处时，她渐渐自在。', '她在你面前可以安心做自己。', '你们的相处让她很安心。'),
        'closeness': ('你们正在寻找合适的距离。', '她开始期待与你分享。', '你已经在她心里占有重要的位置。', '你们珍惜彼此的陪伴。'),
        'tension': ('你们之间没有明显的紧张。', '偶尔的小摩擦仍需要耐心。', '有些事还需要好好谈谈。', '她需要被尊重，也需要一点缓和的空间。'),
    }.items()},
    'moments': {'disclosure_personal': '你让她多了解了一点自己', 'disclosure_vulnerable': '你跟她说了心里话',
        'care_user': '你认真地关心了她', 'care_her': '她接住了你的情绪', 'plan': '你们定下了一个约定',
        'kept': '你们的约定有了后续', 'created': '你们一起完成了一件事', 'affection': '你表达了对她的在意',
        'boundary': '你们尊重了彼此的边界', 'repair': '你们化解了一次矛盾', 'conflict': '你们之间发生了一次冲突'},
}


def validate_texts(value, default):
    if type(default) is bool:
        if type(value) is not bool:
            raise ValueError('CLIENT_POLICY_INVALID')
        return value
    if isinstance(default, str):
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 4000:
            raise ValueError('CLIENT_POLICY_INVALID')
        return value
    if not isinstance(value, dict) or set(value) - default.keys():
        raise ValueError('CLIENT_POLICY_INVALID')
    return {key: validate_texts(value[key], text) if key in value else text for key, text in default.items()}


def volume_catalog(state, now):
    from runtime.cloud_policy import get_policy
    from runtime.relationship_growth import route
    catalog = get_policy('relationship_texts')['volumes'][route(state)]
    result = dict(catalog[str(min(6, state.relationship_volume))])
    if state.relationship_volume >= 6:
        result['title'] = ('冬天', '春天', '夏天', '秋天')[(now.month % 12) // 3]
    return result
