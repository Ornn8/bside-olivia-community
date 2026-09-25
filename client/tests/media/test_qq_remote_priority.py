from runtime import remote_pipeline


def test_only_qq_tts_carries_priority_channel(monkeypatch, tmp_path):
    calls = []
    class Remote:
        def __init__(self, *args): pass
        async def generate(self, kind, data, output, **kwargs):
            calls.append((kind, data))
            return {'task_id': 'synthetic'}
    monkeypatch.setattr('runtime.remote_generation.RemoteGeneration', Remote)
    payload = {'text': 'hello'}
    for kind, env in [('tts', {'OLIVIA_MEDIA_CHANNEL': 'qq'}), ('tts', {}),
                      ('video', {'OLIVIA_MEDIA_CHANNEL': 'qq'})]:
        remote_pipeline.generate(kind, payload, tmp_path / 'out', environment=env)
    assert calls == [('tts', {'text': 'hello', 'channel': 'qq'}),
                     ('tts', payload), ('video', payload)]
    assert payload == {'text': 'hello'}
