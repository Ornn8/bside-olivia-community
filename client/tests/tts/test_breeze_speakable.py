"""Dashes and paragraph breaks are read as pauses, never as the end of the reply."""
from tts import external_breeze_worker as worker


def test_dash_becomes_a_pause():
    text = '你手伸得快它照样炸毛。至于小财迷——那只蝴蝶要是会说话，估计得骂街。'
    assert worker._speakable(text) == '你手伸得快它照样炸毛。至于小财迷，那只蝴蝶要是会说话，估计得骂街。'


def test_paragraph_break_after_a_full_stop_keeps_the_full_stop():
    assert worker._speakable('先把那口气呼完。\n\n抱抱收到。') == '先把那口气呼完。抱抱收到。'


def test_paragraph_break_without_punctuation_becomes_a_comma():
    assert worker._speakable('大飞\n\n早安。') == '大飞，早安。'


def test_plain_text_is_unchanged():
    text = '行啊，等它真进门了你就来当食品投喂机。'
    assert worker._speakable(text) == text


def test_every_chunk_is_generated_from_speakable_text(monkeypatch):
    seen = []
    def generate(bundle, **kwargs):
        seen.append(kwargs['text'])
        return {'sample_rate': 24000, 'waveform': None}
    worker._generate_complete_audio(generate, object(), text='甲——乙。', chunks=['甲——乙。'],
                                    audio_only_unbounded=False, instruction='')
    assert seen == ['甲，乙。']
