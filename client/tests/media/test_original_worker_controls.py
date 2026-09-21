import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

@pytest.mark.parametrize('planning',[False,True])
def test_real_worker_options_adapter_restore_and_cached_lm(tmp_path,monkeypatch,planning):
    torch=pytest.importorskip('torch')
    import numpy as np
    import soundfile as sf
    from runtime.media import ace_cover_worker as worker
    from runtime.media.music_options import generation_parameters
    layer=torch.nn.Module();layer.lora_B=torch.nn.ModuleDict({'original':torch.nn.Linear(128,2,bias=False)})
    before=layer.lora_B['original'].weight.detach().clone()
    decoder=SimpleNamespace(active_adapters=['original'],modules=lambda:[layer],set_adapter=lambda _:None)
    handler=SimpleNamespace(model=SimpleNamespace(decoder=decoder),generate_instruction=lambda **k:'synthetic')
    cache={'models':(handler,[],[layer])};calls=[];initializations=[]
    class Params(SimpleNamespace):
        def to_dict(self):return vars(self)
    class LM:
        def initialize(self,**kwargs):initializations.append(kwargs);return 'ready',True
    def generate(handler,lm,params,config,save_dir,progress):
        assert torch.count_nonzero(layer.lora_B['original'].weight[:,:64])==0
        assert torch.equal(layer.lora_B['original'].weight[:,64:],before[:,64:])
        calls.append((lm,params,config));progress(.9,'decoding')
        Path(save_dir).mkdir(parents=True);sf.write(Path(save_dir)/'song.flac',np.zeros(1600),16000)
        return SimpleNamespace(success=True)
    monkeypatch.setitem(sys.modules,'acestep',SimpleNamespace())
    monkeypatch.setitem(sys.modules,'acestep.inference',SimpleNamespace(GenerationParams=Params,GenerationConfig=Params,generate_music=generate))
    monkeypatch.setitem(sys.modules,'acestep.llm_inference',SimpleNamespace(LLMHandler=LM))
    monkeypatch.setattr(torch.cuda,'is_available',lambda:True)
    monkeypatch.setattr(torch.cuda,'reset_peak_memory_stats',lambda:None)
    monkeypatch.setattr(torch.cuda,'max_memory_allocated',lambda:0)
    monkeypatch.setattr(torch.cuda,'manual_seed_all',lambda _:None)
    (tmp_path/'checkpoints/acestep-5Hz-lm-1.7B').mkdir(parents=True)
    request=dict(runtime_root=str(tmp_path),task_type='text2music',lyrics='synthetic',reference='reference.wav',
        output=str(tmp_path/'result.wav'),**generation_parameters({'guidance_scale':9,'seed':42,
        'use_cot':planning,'thinking':planning,'caption':'guitar'},'fallback'))
    path=tmp_path/'request.json';path.write_text(json.dumps(request))
    for _ in range(2):worker.run(path,cache=cache,resident_options={'_resident_adapter':'original'})
    assert len(initializations)==int(planning)
    assert (calls[0][0] is not None)==planning
    assert calls[0][1].guidance_scale==9 and calls[0][1].use_adg is True
    assert calls[0][1].use_cot_caption is planning and calls[0][1].thinking is planning
    assert calls[0][1].caption=='guitar' and calls[0][2].seeds==[42]
    assert torch.equal(layer.lora_B['original'].weight,before)
    with pytest.raises(RuntimeError):
        with worker.singing_only(decoder,True):raise RuntimeError('generation failed')
    assert torch.equal(layer.lora_B['original'].weight,before)
