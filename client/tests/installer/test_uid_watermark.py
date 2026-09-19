from patch_feapp import hide_uid_watermark


def test_only_uid_overlay_is_hidden_and_reapplication_is_noop(tmp_path):
    styles = tmp_path / 'assets'
    styles.mkdir()
    css = styles / 'main.css'
    original = '.scene{color:red}.watermark-overlay{position:fixed;pointer-events:none}'
    css.write_text(original, encoding='utf-8')
    untouched = styles / 'other.css'
    untouched.write_text('.logo{display:block}', encoding='utf-8')
    assert hide_uid_watermark(tmp_path)
    assert css.read_text() == original.replace('.watermark-overlay{', '.watermark-overlay{display:none!important;')
    assert untouched.read_text() == '.logo{display:block}'
    assert not hide_uid_watermark(tmp_path)
