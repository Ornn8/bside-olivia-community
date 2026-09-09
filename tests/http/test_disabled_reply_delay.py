import json

import local_server


def test_disabled_delay_releases_existing_completed_letter(tmp_path, monkeypatch):
    letter={'letter_id':'delay-test','letter_status':'COMPLETED','reply_mode':'text_letter',
            'reply_text':'already generated','reply_not_before':4_000_000_000,'reply_delay_minutes':8}
    (tmp_path/'state.json').write_text(json.dumps({'letters':[letter]}))
    monkeypatch.setenv('OLIVIA_REPLY_DELAY_ENABLED','0')
    monkeypatch.setattr(local_server,'_state_root',lambda:tmp_path)
    monkeypatch.setattr(local_server.store,'letters',[])
    monkeypatch.setattr(local_server,'_persist_store_state',lambda:None)
    local_server._load_store_state()
    loaded=local_server.store.letters[0]
    assert loaded['reply_not_before']==0
    assert loaded['reply_text']=='already generated'
    assert loaded['letter_status']=='COMPLETED'
    local_server._schedule_text_reply_delay(loaded,'text_letter')
    assert loaded['reply_not_before']==0
