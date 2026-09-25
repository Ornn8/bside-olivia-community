import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from installer.patch_letter_stickers import patch_letter_stickers, patch_source


def test_patch_preserves_unrelated_members_and_is_idempotent(tmp_path):
    source=('content:e.replyText??"",;__name:"MailBoxReplyContent",props:{;'
            'class:ae(["mail-box-reply-content faux-bold",o(E)]);'
            'ref:p,value:l.modelValue,readonly:A.readonly,;'
            'n("div",mw,v(o(I)),1);null,42,uw);],2)}}});const $s=;'
            '__name:"MailBoxContentBody";F(ks,{onVideoError:u},null,8,[]);F(ks,{onVideoError:u},null,8,[])]}),_:1},8,["disabled"])')
    path=tmp_path/'feapp.dat'
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('assets/main-31595bd3.js',source)
        z.writestr('untouched.bin',b'preserve this')
    assert patch_letter_stickers(path)=='PATCHED'
    with zipfile.ZipFile(path) as z:
        assert z.read('untouched.bin')==b'preserve this'
        patched=z.read('assets/main-31595bd3.js').decode()
        assert 'null,46,uw)' in patched and '],6)}}});' in patched
        assert 'value:A.type==="text"&&A.signature&&typeof A.bodyText==="string"?A.bodyText:l.modelValue' in patched
        assert patched.count('signature:i.mail.received?.signature') == 2
        assert patched.count('["bodyText","signature","stickerId",') == 2
        assert 'class:"olivia-letter-signature"},v(A.signature),1)' in patched
        assert sum(n.endswith('.png') for n in z.namelist())==252
        assert sum(n.endswith('.gif') for n in z.namelist())==20
        assert 'oliviaLetterStickerAsset(A.stickerId)' in patched
    before=path.read_bytes()
    assert patch_letter_stickers(path)=='ALREADY_PATCHED'
    assert path.read_bytes()==before

    from installer.patch_letter_stickers import patch_photos, PHOTO_MARKER, LEGACY_PHOTO_MARKER
    attachment = ',i.mail.received?.content&&i.mail.received?.imageRequestId?n("olivia-photo",{"letter-id":i.mail.received.imageRequestId},null,8,["letter-id"]):Y("",!0)'
    assert attachment in patched
    legacy = patched.replace(PHOTO_MARKER, LEGACY_PHOTO_MARKER).replace(attachment, '')
    legacy = legacy.replace('n("div",mw,v(o(I)),1)', 'A.imageRequestId?n("olivia-photo",{"letter-id":A.imageRequestId},null,8,["letter-id"]):Y("",!0),n("div",mw,v(o(I)),1)')
    assert patch_photos(legacy) == patched

    # Upgrade an already-patched archive without reinjecting native props.
    old=tmp_path/'old54.dat'
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(old,'w') as dst:
        for info in src.infolist():
            if info.filename.endswith('.png') and int(info.filename.rsplit('-',1)[1][:-4])>54:
                continue
            dst.writestr(info,src.read(info))
    assert patch_letter_stickers(old)=='PATCHED'
    with zipfile.ZipFile(old) as z:
        assert z.read('assets/main-31595bd3.js').decode()==patched
        assert 'assets/letter-stickers/linli-272.gif' in z.namelist()
    assert patch_letter_stickers(old)=='ALREADY_PATCHED'

    # Existing v4 installations must switch native rendering from PNG-only to extension-aware assets.
    from installer.patch_letter_stickers import MARKER, PREVIOUS_MARKER
    v4=tmp_path/'v4.dat'
    legacy_js=(patched.replace(MARKER,PREVIOUS_MARKER,1)
               .replace('oliviaLetterStickerAsset(A.stickerId)',
                        'oliviaLetterSticker(A.stickerId)+".png"',1)
               .replace('import{oliviaLetterStickerAsset}from',
                        'import{oliviaLetterSticker}from',1))
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(v4,'w') as dst:
        for info in src.infolist():
            dst.writestr(info,legacy_js if info.filename=='assets/main-31595bd3.js' else src.read(info))
    assert patch_letter_stickers(v4)=='PATCHED'
    with zipfile.ZipFile(v4) as z:
        assert z.read('assets/main-31595bd3.js').decode()==patched


def test_unknown_frontend_is_rejected_without_writing(tmp_path):
    archive = tmp_path / 'feapp.dat'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('assets/main-31595bd3.js', 'unknown version')
    before = archive.read_bytes()
    with pytest.raises(ValueError, match='STICKER_ANCHOR'):
        patch_letter_stickers(archive)
    assert archive.read_bytes() == before


def test_frontend_uses_metadata_and_rejects_invalid_paths():
    script = Path('runtime/letter_stickers/select.js').resolve()
    program = '''const fs=require('fs'),vm=require('vm');
const s=fs.readFileSync(process.argv[1],'utf8').replaceAll('export function','function');
const ctx={};vm.createContext(ctx);vm.runInContext(s,ctx);
const cases=['linli-07','linli-108','linli-109','linli-253','linli-272','../../key.txt','linli-273','linli-0108'];
console.log(JSON.stringify(cases.map(t=>[ctx.oliviaLetterSticker(t),ctx.oliviaLetterStickerAsset(t)])));
'''
    rows = json.loads(subprocess.check_output(['node','-e',program,str(script)],text=True,encoding='utf-8'))
    assert rows[0][0]=='linli-07'
    assert rows[1][0]=='linli-108'
    assert all(row == ['linli-01','linli-01.png'] for row in rows[2:5])
    assert all(row == ['linli-01','linli-01.png'] for row in rows[5:])


def test_assets_are_valid_images_with_catalog_entries():
    from PIL import Image
    root=Path('runtime/letter_stickers')
    files=list(root.glob('linli-*.png'))
    gifs=list(root.glob('linli-*.gif'))
    assert len(files)==252 and len(gifs)==20
    catalog=json.loads((root/'catalog.json').read_text(encoding='utf-8'))
    assert {item['file'] for item in catalog} == {f.name for f in files+gifs}
    for file in files:
        with Image.open(file) as image:
            image.verify()
    for file in gifs:
        with Image.open(file) as image:
            assert image.n_frames > 1
