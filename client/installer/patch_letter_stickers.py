"""Add local line illustrations to the native 0627 reply component atomically."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import zipfile

MAIN='assets/main-31595bd3.js'
MARKER='/*olivia-letter-stickers-v5*/'
FLOW_MARKER='/*olivia-letter-stickers-v6*/'
PAPER_MARKER='/*olivia-letter-paper-v1*/'
SCROLL_MARKER='/*olivia-letter-scroll-v1*/'
PREVIOUS_MARKER='/*olivia-letter-stickers-v4*/'
PHOTO_MARKER='/*olivia-letter-photos-v2*/'
VIDEO_TEXT_MARKER='/*olivia-letter-video-text-v1*/'
LEGACY_PHOTO_MARKER='/*olivia-letter-photos-v1*/'
METADATA_MARKER='/*olivia-letter-stickers-v3*/'
LAYOUT_MARKER='/*olivia-letter-stickers-v2*/'
ASSETS=Path(__file__).resolve().parents[1]/'runtime'/'letter_stickers'


def _patch_layout(source: str) -> str:
    if LAYOUT_MARKER in source:
        return source
    anchors = {
        'class:ae(["mail-box-reply-content faux-bold",o(E)])':
        'style:o(g)?{height:"auto",aspectRatio:"auto",minHeight:"290px"}:null,class:ae(["mail-box-reply-content faux-bold",o(E)])',
        'ref:p,value:l.modelValue,readonly:A.readonly,':
        'ref:p,style:o(g)?{height:((p.value&&p.value.scrollHeight)||230)+"px",flex:"none"}:null,value:l.modelValue,readonly:A.readonly,',
        'n("div",mw,v(o(I)),1)':
        'A.type==="text"&&A.modelValue?n("img",{class:"olivia-letter-sticker",src:new URL("./letter-stickers/"+oliviaLetterSticker(A.modelValue)+".png",import.meta.url).href,alt:"",draggable:"false"},null,8,["src"]):Y("",!0),n("div",mw,v(o(I)),1)',
    }
    for before, after in anchors.items():
        if source.count(before)!=1:
            raise ValueError('STICKER_ANCHOR_INVALID')
        source=source.replace(before,after,1)
    start=source.index('__name:"MailBoxReplyContent"')
    end=source.index('const $s=',start)
    component=source[start:end]
    if component.count('],2)}}});')!=1 or component.count('null,42,uw)')!=1:
        raise ValueError('STICKER_ANCHOR_CAPTURE_INVALID')
    component=component.replace('],2)}}});','],6)}}});').replace('null,42,uw)','null,46,uw)')
    source=source[:start]+component+source[end:]
    return LAYOUT_MARKER+'import{oliviaLetterSticker}from"./letter-stickers/select.js";'+source


def patch_source(source: str) -> str:
    source = _patch_flow(_patch_stickers(source))
    if SCROLL_MARKER in source:
        return source
    if PAPER_MARKER in source:
        for anchor in [PAPER_MARKER,
                       'import{oliviaLetterPaperMounted,oliviaLetterPaperUnmounted}from"./letter-stickers/select.js";',
                       'onVnodeMounted:oliviaLetterPaperMounted,onVnodeUnmounted:oliviaLetterPaperUnmounted,']:
            if source.count(anchor) != 1:
                raise ValueError('STICKER_PAPER_UPGRADE_INVALID')
            source = source.replace(anchor, '', 1)
    anchors = {
        '{"olivia-letter-flow":A.type==="text"&&A.modelValue&&A.readonly}':
        '{"olivia-letter-flow":A.type==="text"&&A.modelValue&&A.readonly,"olivia-letter-capture":o(g)}',
        '_(se,{key:2},[A.type==="text"&&A.readonly?':
        '_(A.type==="text"&&A.readonly?"div":se,{key:2,class:A.type==="text"&&A.readonly?"olivia-letter-scroll":null},[A.type==="text"&&A.readonly?',
    }
    for before, after in anchors.items():
        if source.count(before) != 1:
            raise ValueError('STICKER_SCROLL_ANCHOR_INVALID')
        source = source.replace(before, after, 1)
    return SCROLL_MARKER + source


def _patch_stickers(source: str) -> str:
    if MARKER in source:
        return source
    if PREVIOUS_MARKER in source:
        return _upgrade_assets(source)
    if METADATA_MARKER in source:
        return _upgrade_assets(_patch_presentation(source))
    source=_patch_layout(source)
    anchors={
        'content:e.replyText??"",':'stickerId:e.replyStickerId||"",content:e.replyText??"",',
        '__name:"MailBoxReplyContent",props:{':'__name:"MailBoxReplyContent",props:{stickerId:{},',
        'oliviaLetterSticker(A.modelValue)':'oliviaLetterSticker(A.stickerId)',
    }
    for before,after in anchors.items():
        if source.count(before)!=1: raise ValueError('STICKER_ANCHOR_METADATA_INVALID')
        source=source.replace(before,after,1)
    if source.count('F(ks,{')!=2 or source.count('onVideoError:u},null,8,[')!=2:
        raise ValueError('STICKER_ANCHOR_PROPS_INVALID')
    source=source.replace('F(ks,{','F(ks,{stickerId:i.mail.received?.stickerId,')
    source=source.replace('onVideoError:u},null,8,[','onVideoError:u},null,8,["stickerId",')
    return _upgrade_assets(_patch_presentation(source.replace(LAYOUT_MARKER,METADATA_MARKER,1)))


def _patch_flow(source: str) -> str:
    if FLOW_MARKER in source:
        return source
    anchors = {
        'class:ae(["mail-box-reply-content faux-bold",o(E)])':
        'class:ae(["mail-box-reply-content faux-bold",o(E),{"olivia-letter-flow":A.type==="text"&&A.modelValue&&A.readonly}])',
        'A.type==="text"?(r(),_("textarea",{key:0,':
        'A.type==="text"&&A.readonly?n("div",{class:"olivia-letter-body"},v(A.signature&&typeof A.bodyText==="string"?A.bodyText:l.modelValue),1):A.type==="text"?(r(),_("textarea",{key:0,',
        'A.type==="text"&&A.modelValue?n("img",{class:"olivia-letter-sticker",':
        'n("div",{class:A.type==="text"?"olivia-letter-footer":null},[A.type==="text"&&A.modelValue?n("img",{class:"olivia-letter-sticker",',
        'n("div",mw,v(o(I)),1)': 'n("div",mw,v(o(I)),1)],2)',
    }
    for before, after in anchors.items():
        if source.count(before) != 1:
            raise ValueError('STICKER_FLOW_ANCHOR_INVALID')
        source = source.replace(before, after, 1)
    return FLOW_MARKER + source


def _upgrade_assets(source: str) -> str:
    old_import='import{oliviaLetterSticker}from"./letter-stickers/select.js";'
    old_call='oliviaLetterSticker(A.stickerId)+".png"'
    if source.count(PREVIOUS_MARKER)!=1 or source.count(old_import)!=1 or source.count(old_call)!=1:
        raise ValueError('STICKER_ASSET_UPGRADE_INVALID')
    return (source.replace(old_import,'import{oliviaLetterStickerAsset}from"./letter-stickers/select.js";',1)
                  .replace(old_call,'oliviaLetterStickerAsset(A.stickerId)',1)
                  .replace(PREVIOUS_MARKER,MARKER,1))


def _patch_presentation(source: str) -> str:
    anchors = {
        'stickerId:e.replyStickerId||"",':
        'bodyText:e.replyBody,signature:e.replySignature||"",stickerId:e.replyStickerId||"",',
        '__name:"MailBoxReplyContent",props:{':
        '__name:"MailBoxReplyContent",props:{bodyText:{},signature:{},',
        'value:l.modelValue,readonly:A.readonly,':
        'value:A.type==="text"&&A.signature&&typeof A.bodyText==="string"?A.bodyText:l.modelValue,readonly:A.readonly,',
        'n("div",mw,v(o(I)),1)':
        'A.type==="text"&&A.modelValue&&A.signature&&typeof A.bodyText==="string"?n("div",{class:"olivia-letter-signature"},v(A.signature),1):Y("",!0),n("div",mw,v(o(I)),1)',
    }
    for before, after in anchors.items():
        if source.count(before) != 1:
            raise ValueError('STICKER_ANCHOR_PRESENTATION_INVALID')
        source = source.replace(before, after, 1)
    if source.count('F(ks,{stickerId:') != 2 or source.count('onVideoError:u},null,8,["stickerId",') != 2:
        raise ValueError('STICKER_ANCHOR_PRESENTATION_PROPS_INVALID')
    source = source.replace('F(ks,{stickerId:', 'F(ks,{bodyText:i.mail.received?.bodyText,signature:i.mail.received?.signature,stickerId:')
    source = source.replace('onVideoError:u},null,8,["stickerId",', 'onVideoError:u},null,8,["bodyText","signature","stickerId",')
    return source.replace(METADATA_MARKER, PREVIOUS_MARKER, 1)


def patch_photos(source: str) -> str:
    if PHOTO_MARKER in source: return source
    if LEGACY_PHOTO_MARKER in source:
        return _move_photo_after_paper(source)
    anchors = {
        'bodyText:e.replyBody,': 'imageRequestId:e.imageRequestId||"",bodyText:e.replyBody,',
        '__name:"MailBoxReplyContent",props:{': '__name:"MailBoxReplyContent",props:{imageRequestId:{},',
        'n("div",mw,v(o(I)),1)':
        'A.imageRequestId?n("olivia-photo",{"letter-id":A.imageRequestId},null,8,["letter-id"]):Y("",!0),n("div",mw,v(o(I)),1)',
    }
    for before, after in anchors.items():
        if source.count(before) != 1: raise ValueError('PHOTO_ANCHOR_INVALID')
        source=source.replace(before,after,1)
    if source.count('F(ks,{bodyText:') != 2: raise ValueError('PHOTO_PROPS_INVALID')
    source=source.replace('F(ks,{bodyText:','F(ks,{imageRequestId:i.mail.received?.imageRequestId,bodyText:')
    source=source.replace('["bodyText","signature","stickerId",','["bodyText","signature","stickerId","imageRequestId",')
    return _move_photo_after_paper(LEGACY_PHOTO_MARKER+source)


def _move_photo_after_paper(source: str) -> str:
    photo = 'A.imageRequestId?n("olivia-photo",{"letter-id":A.imageRequestId},null,8,["letter-id"]):Y("",!0),'
    if source.count(photo) != 1:
        raise ValueError('PHOTO_PLACEMENT_INVALID')
    source = source.replace(photo, '', 1)
    start = source.index('__name:"MailBoxContentBody"')
    end = source.find('__name:', start + 10)
    end = len(source) if end < 0 else end
    component = source[start:end]
    anchor = ']}),_:1},8,["disabled"])'
    if component.count(anchor) != 1:
        raise ValueError('PHOTO_ATTACHMENT_SLOT_INVALID')
    attachment = ',i.mail.received?.content&&i.mail.received?.imageRequestId?n("olivia-photo",{"letter-id":i.mail.received.imageRequestId},null,8,["letter-id"]):Y("",!0)'
    component = component.replace(anchor, attachment + anchor, 1)
    return (source[:start] + component + source[end:]).replace(LEGACY_PHOTO_MARKER, PHOTO_MARKER, 1)


# The settings patch (2.1.1) puts the reply failure code first in the paper's props.
# The sticker and photo anchors expect their own props first, so a client whose
# stickers were already patched but whose photos were not could no longer start
# (CLIENT_FRONTEND_REPAIR_FAILED / PHOTO_PROPS_INVALID). Patch without it, then
# put it back exactly where it was.
FAILURE_PROP='F(ks,{errorCode:i.mail.errorCode,'


def _patch_with_failure_prop(source: str) -> str:
    count=source.count(FAILURE_PROP)
    if count:
        source=source.replace(FAILURE_PROP,'F(ks,{')
    source=patch_photos(patch_source(source))
    if count:
        if source.count('F(ks,{')!=count: raise ValueError('STICKER_FAILURE_PROP_INVALID')
        source=source.replace('F(ks,{',FAILURE_PROP)
    return source


# A video reply (a cover, a song, her speaking) showed only the video: the letter she wrote with it could no
# longer be read once the video arrived (2026-10-19). Her letter now follows the video on its own paper.
VIDEO_TEXT_ANCHOR = '8,["disabled"]),o(m)?(r(),F($s,{key:0,class:"mail-responsive-card"}'
VIDEO_TEXT_PAPER = (',i.mail.received?.type==="video"&&i.mail.received?.content?(r(),_("div",{key:"olivia-video-letter",'
    'class:"mail-responsive-card olivia-video-letter"},[k(ks,({bodyText:i.mail.received.bodyText,signature:i.mail.received.signature,'
    'stickerId:i.mail.received.stickerId,modelValue:i.mail.received.content,class:"mail-card-content","is-visible":!0,'
    'readonly:"",timestamp:i.mail.received.timestamp??null,type:"text"}),null,8,'
    '["bodyText","signature","stickerId","modelValue","timestamp"])])):Y("",!0)')


def _patch_video_text(source: str) -> str:
    """Optional: a client whose bundle differs keeps the video-only paper rather than failing to start."""
    if VIDEO_TEXT_MARKER in source or '__name:"MailBoxContentBody"' not in source:
        return source
    start = source.index('__name:"MailBoxContentBody"')
    end = source.find('__name:', start + 10)
    end = len(source) if end < 0 else end
    component = source[start:end]
    if component.count(VIDEO_TEXT_ANCHOR) != 1:
        return source
    head = '8,["disabled"])'
    component = component.replace(VIDEO_TEXT_ANCHOR, head + VIDEO_TEXT_PAPER + VIDEO_TEXT_ANCHOR[len(head):], 1)
    return VIDEO_TEXT_MARKER + source[:start] + component + source[end:]


# Pictures the user puts in a letter (2.5.0) lie under the user's own paper the way her photo lies under hers:
# the same <olivia-photo> print, given the picture directly. Four fixed slots keep the paper's child count stable.
SENT_PHOTOS_MARKER = '/*olivia-letter-sent-photos-v1*/'
SENT_PHOTOS_MAIL = {
    'detailLoaded:!0,sent:{subject:t,': 'detailLoaded:!0,sentImages:e.sentImageUrls||[],sent:{subject:t,',
    'Ee.detailLoaded&&(ye.detailLoaded=!0)': 'Ee.detailLoaded&&(ye.detailLoaded=!0,ye.sentImages=Ee.sentImages)',
}
SENT_PAPER_ANCHOR = '["modelValue","timestamp","stamp-id"]))]'
SENT_PHOTOS = ''.join(f',i.mail.sentImages?.[{index}]?n("olivia-photo",{{src:i.mail.sentImages[{index}],'
                      f'"data-index":"{index}"}},null,8,["src"]):Y("",!0)' for index in range(4))


def _patch_sent_photos(source: str) -> str:
    """Optional: a client whose bundle differs shows the user's paper without pictures rather than failing to start."""
    if SENT_PHOTOS_MARKER in source or '__name:"MailBoxContentBody"' not in source:
        return source
    if any(source.count(anchor) != 1 for anchor in SENT_PHOTOS_MAIL):
        return source
    start = source.index('__name:"MailBoxContentBody"')
    end = source.find('__name:', start + 10)
    end = len(source) if end < 0 else end
    component = source[start:end]
    if component.count(SENT_PAPER_ANCHOR) != 1:
        return source
    component = component.replace(SENT_PAPER_ANCHOR, SENT_PAPER_ANCHOR[:-1] + SENT_PHOTOS + ']', 1)
    source = source[:start] + component + source[end:]
    for before, after in SENT_PHOTOS_MAIL.items():
        source = source.replace(before, after, 1)
    return SENT_PHOTOS_MARKER + source


def patch_letter_stickers(path: Path | str) -> str:
    path=Path(path)
    from installer.full_patch import DEFAULT_STICKER_FILES
    assets={f'assets/letter-stickers/{p.name}':p.read_bytes() for p in ASSETS.iterdir()
            if p.suffix in {'.json','.js'} or p.name in DEFAULT_STICKER_FILES}
    expected={f'assets/letter-stickers/{name}' for name in DEFAULT_STICKER_FILES}
    with zipfile.ZipFile(path) as archive:
        if MAIN not in archive.namelist():
            return 'UNSUPPORTED_CLIENT'
        source=archive.read(MAIN).decode('utf-8')
        if not expected <= (assets.keys() | set(archive.namelist())):
            raise ValueError('STICKER_ASSETS_INCOMPLETE')
        obsolete={name for name in archive.namelist() if name.startswith('assets/letter-stickers/')
                  and name.endswith(('.png','.gif')) and name not in expected}
        obsolete.update(f'assets/letter-stickers/paper-{part}.svg' for part in ('top','middle','bottom')
                        if f'assets/letter-stickers/paper-{part}.svg' in archive.namelist())
        changed=_patch_sent_photos(_patch_video_text(_patch_with_failure_prop(source))).encode('utf-8')
        if not obsolete and changed==source.encode('utf-8') and all(n in archive.namelist() and archive.read(n)==data for n,data in assets.items()):
            return 'ALREADY_PATCHED'
        with tempfile.TemporaryDirectory(prefix='.letter-stickers-',dir=path.parent) as folder:
            staged=Path(folder)/'feapp.dat'
            with zipfile.ZipFile(staged,'w',zipfile.ZIP_DEFLATED) as target:
                for info in archive.infolist():
                    if info.filename not in assets and info.filename not in obsolete:
                        target.writestr(info,changed if info.filename==MAIN else archive.read(info))
                for name,data in assets.items(): target.writestr(name,data)
            with zipfile.ZipFile(staged) as verify:
                if verify.testzip() is not None: raise ValueError('STICKER_ARCHIVE_INVALID')
            backup=path.with_name(path.name+'.stickers.orig')
            if not backup.exists(): shutil.copy2(path,backup)
            # Windows requires releasing the source ZIP before replacement.
            archive.close()
            os.replace(staged,path)
    return 'PATCHED'
