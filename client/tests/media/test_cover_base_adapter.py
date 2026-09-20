from contextlib import contextmanager
import pytest
from runtime.media.ace_cover_worker import generation_adapter


@pytest.mark.parametrize('fails', [False, True])
def test_cover_disables_adapter_and_restores_original_even_after_failure(fails):
    class Decoder:
        enabled = True
        @contextmanager
        def disable_adapter(self):
            self.enabled = False
            try: yield
            finally: self.enabled = True
    decoder = Decoder()
    try:
        with generation_adapter(decoder, 'cover'):
            assert not decoder.enabled
            if fails: raise RuntimeError('synthetic')
    except RuntimeError: pass
    with generation_adapter(decoder, 'text2music'):
        assert decoder.enabled
