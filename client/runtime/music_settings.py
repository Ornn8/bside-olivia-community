"""Locally persisted original music preferences, independent of GPU credentials."""
import json
import os
from pathlib import Path
from runtime.cloud_service import CloudError
from runtime.media.music_options import DEFAULTS, validate

class MusicSettings:
    def __init__(self,root,environment=None):
        self.path=Path(root)/'original-music-settings.json'
        self.environment=os.environ if environment is None else environment
        self.error=''
    def load(self):
        try:
            if self.path.exists() and self.path.stat().st_size>32768: raise ValueError()
            value=validate(json.loads(self.path.read_text(encoding='utf-8'))) if self.path.exists() else dict(DEFAULTS)
            self.environment['OLIVIA_ORIGINAL_MUSIC_OPTIONS']=json.dumps(value,ensure_ascii=False)
        except (OSError,ValueError,TypeError):
            self.error='MUSIC_SETTINGS_UNAVAILABLE'
            self.environment['OLIVIA_ORIGINAL_MUSIC_OPTIONS']='null'
    def status(self):
        return {'status':'OK','options':None if self.error else validate(json.loads(self.environment.get('OLIVIA_ORIGINAL_MUSIC_OPTIONS','{}'))),
                'defaults':dict(DEFAULTS),'error_code':self.error}
    def save(self,value):
        try: value=validate(value)
        except (ValueError,TypeError): raise CloudError('MUSIC_OPTIONS_INVALID',400) from None
        text=json.dumps(value,ensure_ascii=False)
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temporary=self.path.with_suffix('.tmp');temporary.write_text(text,encoding='utf-8');temporary.replace(self.path)
        except OSError: raise CloudError('MUSIC_SETTINGS_SAVE_FAILED') from None
        self.environment['OLIVIA_ORIGINAL_MUSIC_OPTIONS']=text;self.error=''
        return self.status()
