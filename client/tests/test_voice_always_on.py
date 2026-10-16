from runtime.video_reply_settings import VideoReplySettingsStore, tier_preferences
from runtime.reply.proactive_letters import settings


def test_legacy_text_setting_cannot_disable_voice(tmp_path):
    store = VideoReplySettingsStore.initialize(tmp_path)
    store.mutate_tier('video_reply_setting:legacy', 'text')
    assert store.routes_snapshot()['voice_reply'] is True
    assert store.routes_snapshot()['singing_video'] is False
    assert store.routes_snapshot()['voice_song_video'] is False
    assert store.tier_snapshot() == 'audio'
    assert not any(store.videos_snapshot().values())
    store.mutate('video_reply_setting:old-toggle', False)
    assert store.routes_snapshot()['voice_reply'] is True
    assert VideoReplySettingsStore(tmp_path).routes_snapshot()['voice_reply'] is True


def test_voice_keeps_video_and_login_choices():
    assert tier_preferences('audio')[1]['voice_reply'] is False
    assert tier_preferences('video')[1]['voice_reply'] is True
    assert settings({'enabled': False, 'allow_voice': False, 'login_check_enabled': True}) == {
        'enabled': True, 'allow_voice': True, 'login_check_enabled': True}
