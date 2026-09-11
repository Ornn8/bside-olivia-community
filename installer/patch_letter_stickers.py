"""Add local line illustrations to the native 0627 reply component atomically."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import zipfile

MAIN='assets/main-31595bd3.js'
MARKER='/*olivia-letter-stickers-v3*/'
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
    if MARKER in source:
        return source
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
    return source.replace(LAYOUT_MARKER,MARKER,1)


def patch_letter_stickers(path: Path | str) -> str:
    path=Path(path)
    assets={f'assets/letter-stickers/{p.name}':p.read_bytes() for p in ASSETS.iterdir() if p.suffix in {'.png','.json','.js'}}
    if sum(n.endswith('.png') for n in assets)!=54:
        raise ValueError('STICKER_ASSETS_INCOMPLETE')
    with zipfile.ZipFile(path) as archive:
        if MAIN not in archive.namelist():
            return 'UNSUPPORTED_CLIENT'
        source=archive.read(MAIN).decode('utf-8')
        changed=patch_source(source).encode('utf-8')
        if changed==source.encode('utf-8') and all(n in archive.namelist() and archive.read(n)==data for n,data in assets.items()):
            return 'ALREADY_PATCHED'
        with tempfile.TemporaryDirectory(prefix='.letter-stickers-',dir=path.parent) as folder:
            staged=Path(folder)/'feapp.dat'
            with zipfile.ZipFile(staged,'w',zipfile.ZIP_DEFLATED) as target:
                for info in archive.infolist():
                    if info.filename not in assets:
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
