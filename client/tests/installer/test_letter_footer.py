import pytest

from installer.patch_letter_stickers import FLOW_MARKER, MARKER, PAPER_MARKER, SCROLL_MARKER, _patch_flow, patch_source


def legacy():
    return (MARKER + 'import{oliviaLetterStickerAsset}from"./letter-stickers/select.js";class:ae(["mail-box-reply-content faux-bold",o(E)]);'
            '_(se,{key:2},[A.type==="text"?(r(),_("textarea",{key:0,;'
            'A.type==="text"&&A.modelValue?n("img",{class:"olivia-letter-sticker",;'
            'n("div",mw,v(o(I)),1)')


def test_v5_upgrade_and_idempotence():
    upgraded = patch_source(legacy())
    assert FLOW_MARKER in upgraded
    assert 'class:"olivia-letter-body"' in upgraded
    assert 'A.bodyText:l.modelValue' in upgraded
    assert 'A.type==="text"?"olivia-letter-footer":null' in upgraded
    assert patch_source(upgraded) == upgraded


@pytest.mark.parametrize('paper', [False, True])
def test_v6_upgrade_and_scroll_anchor_checks(paper):
    previous = _patch_flow(legacy())
    if paper:
        previous = PAPER_MARKER + previous.replace('class:ae(', 'onVnodeMounted:oliviaLetterPaperMounted,onVnodeUnmounted:oliviaLetterPaperUnmounted,class:ae(', 1)
        previous += 'import{oliviaLetterPaperMounted,oliviaLetterPaperUnmounted}from"./letter-stickers/select.js";'
    upgraded = patch_source(previous)
    assert SCROLL_MARKER in upgraded
    assert PAPER_MARKER not in upgraded
    assert 'oliviaLetterPaperMounted' not in upgraded
    assert 'olivia-letter-scroll' in upgraded
    assert patch_source(upgraded) == upgraded
    anchor = 'class:ae(["mail-box-reply-content faux-bold",o(E),{"olivia-letter-flow":A.type==="text"&&A.modelValue&&A.readonly}])'
    for invalid in [previous.replace(anchor, ''), previous + anchor]:
        with pytest.raises(ValueError, match='STICKER_SCROLL_ANCHOR_INVALID'):
            patch_source(invalid)


def test_missing_or_duplicate_scroll_wrapper_anchor_rejected():
    previous = _patch_flow(legacy())
    anchor = '_(se,{key:2},[A.type==="text"&&A.readonly?'
    for invalid in [previous.replace(anchor, ''), previous + anchor]:
        with pytest.raises(ValueError, match='STICKER_SCROLL_ANCHOR_INVALID'):
            patch_source(invalid)


@pytest.mark.parametrize('anchor', [
    'class:ae(["mail-box-reply-content faux-bold",o(E)])',
    'A.type==="text"?(r(),_("textarea",{key:0,',
    'A.type==="text"&&A.modelValue?n("img",{class:"olivia-letter-sticker",',
    'n("div",mw,v(o(I)),1)',
])
@pytest.mark.parametrize('duplicate', [False, True])
def test_flow_anchor_mismatch_is_rejected(anchor, duplicate):
    source = legacy() + anchor if duplicate else legacy().replace(anchor, '')
    with pytest.raises(ValueError, match='STICKER_FLOW_ANCHOR_INVALID'):
        _patch_flow(source)
