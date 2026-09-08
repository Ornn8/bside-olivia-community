"""Optional local Breeze LoRA; floating residual over the unchanged INT8 base."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def adapter_metadata(directory: str | Path) -> dict[str, object]:
    if not directory:
        return {}
    try:
        root = Path(directory).resolve()
        value = json.loads((root / 'adapter_config.json').read_text(encoding='utf-8'))
        spec = value['lora']
        if (value['schema_version'] != 1 or value['artifact_type'] != 'breeze_lora_adapter'
                or value['base_model']['id'] != 'BreezeBlue/Breeze-TTS-2'
                or value['base_model']['revision'] != 'c1c8ca18b70b30822735633991d9ebf4898e47d4'
                or spec != {'variant': 'backbone_depth_projection', 'rank': 8, 'alpha': 16.0, 'seed': 42}
                or value['adapter']['file'] != 'adapter.safetensors'):
            raise ValueError
        artifact = (root / 'adapter.safetensors').resolve()
        if artifact.parent != root:
            raise ValueError
        digest = hashlib.sha256()
        with artifact.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != value['adapter']['sha256']:
            raise ValueError
        return {'sha256': digest.hexdigest(), 'rank': 8, 'alpha': 16.0,
                'base_revision': value['base_model']['revision']}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError('BREEZE_ADAPTER_INVALID') from exc


def make_additive_lora(base, a, b, scale):
    # Torch is only imported in the isolated GPU worker, never in the web backend.
    import torch
    from torch import nn

    class AdditiveLoRA(nn.Module):
        def __init__(self):
            super().__init__()
            self.base = base
            self.lora_A = nn.Parameter(a.to(base.weight.device), requires_grad=False)
            self.lora_B = nn.Parameter(b.to(base.weight.device), requires_grad=False)
            self.in_features, self.out_features = base.in_features, base.out_features

        @property
        def weight(self):
            return self.base.weight

        @property
        def bias(self):
            return self.base.bias

        def forward(self, inputs=None, *, inputs_embeds=None, position_ids=None):
            if inputs is None:
                inputs = inputs_embeds
            elif inputs_embeds is not None:
                raise ValueError('BREEZE_ADAPTER_INVALID')
            original = self.base(inputs)
            update = torch.nn.functional.linear(inputs.to(self.lora_A.dtype), self.lora_A)
            update = torch.nn.functional.linear(update, self.lora_B)
            return original + (update * scale).to(original.dtype)

    return AdditiveLoRA()


def apply_adapter(model, directory, int8_class, expected_sha256):
    import torch
    from torch import nn
    from safetensors.torch import load_file

    metadata = adapter_metadata(directory)
    if not metadata or metadata['sha256'] != expected_sha256:
        raise ValueError('BREEZE_ADAPTER_INVALID')
    state = load_file(str(Path(directory) / 'adapter.safetensors'), device='cpu')
    modules = dict(model.named_modules())
    names = [key[:-7] for key in state if key.endswith('.lora_A')]
    if len(names) != 283 or set(state) != {name + suffix for name in names for suffix in ('.lora_A', '.lora_B')}:
        raise ValueError('BREEZE_ADAPTER_INVALID')
    replacements = []
    counts = {'int8': 0, 'float': 0}
    for name in names:
        base = modules.get(name)
        if not isinstance(base, (nn.Linear, int8_class)):
            raise ValueError('BREEZE_ADAPTER_INVALID')
        a, b = state[name + '.lora_A'], state[name + '.lora_B']
        if (a.shape != (8, base.in_features) or b.shape != (base.out_features, 8)
                or a.dtype != torch.bfloat16 or b.dtype != torch.bfloat16
                or not torch.isfinite(a).all() or not torch.isfinite(b).all()):
            raise ValueError('BREEZE_ADAPTER_INVALID')
        counts['int8' if isinstance(base, int8_class) else 'float'] += 1
        replacements.append((name, make_additive_lora(base, a, b, 2.0)))
    for name, wrapper in replacements:
        parent, _, child = name.rpartition('.')
        setattr(modules[parent] if parent else model, child, wrapper)
    return {'sha256': metadata['sha256'], 'targets': counts}
