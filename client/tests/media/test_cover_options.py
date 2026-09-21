import pytest
from runtime.media.cover_options import validate


@pytest.mark.parametrize('bad', [None, [], {'unexpected': .5}, {'audio_cover_strength': True}, {'cover_noise_strength': float('nan')}, {'cover_noise_strength': 1.01}, {'audio_cover_strength': -.1}])
def test_reject_invalid_options(bad):
    with pytest.raises(ValueError):validate(bad)


def test_cover_options_reach_remote_request(tmp_path, monkeypatch):
    from runtime.media.ace_cover import generate_cover
    source=tmp_path/'song.wav';source.write_bytes(b'synthetic')
    seen={}
    monkeypatch.setattr('runtime.remote_pipeline.enabled',lambda env:True)
    def generate(kind,data,*args,**kwargs):seen.update(data);return {}
    monkeypatch.setattr('runtime.remote_pipeline.generate',generate)
    options={'audio_cover_strength':.4,'cover_noise_strength':.2}
    generate_cover(source,tmp_path/'out.wav',environment={},cover_options=options)
    assert seen['cover_options']==options
    assert validate({})=={'audio_cover_strength':.6,'cover_noise_strength':.25}
