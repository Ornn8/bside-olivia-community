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
            'F(ks,{onVideoError:u},null,8,[]);F(ks,{onVideoError:u},null,8,[])')
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
        assert sum(n.endswith('.png') for n in z.namelist())==108
    before=path.read_bytes()
    assert patch_letter_stickers(path)=='ALREADY_PATCHED'
    assert path.read_bytes()==before

    # Upgrade an already-patched 54-image archive without reinjecting native props.
    old=tmp_path/'old54.dat'
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(old,'w') as dst:
        for info in src.infolist():
            if info.filename.endswith('.png') and int(info.filename.rsplit('-',1)[1][:-4])>54:
                continue
            dst.writestr(info,src.read(info))
    assert patch_letter_stickers(old)=='PATCHED'
    with zipfile.ZipFile(old) as z:
        assert z.read('assets/main-31595bd3.js').decode()==patched
        assert 'assets/letter-stickers/linli-108.png' in z.namelist()
    assert patch_letter_stickers(old)=='ALREADY_PATCHED'


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
const s=fs.readFileSync(process.argv[1],'utf8').replace('export function','function');
const ctx={};vm.createContext(ctx);vm.runInContext(s,ctx);
const cases=['linli-07','linli-108','../../key.txt','linli-109','linli-0108'];
console.log(JSON.stringify(cases.map(t=>[ctx.oliviaLetterSticker(t),ctx.oliviaLetterSticker(t)])));
'''
    rows = json.loads(subprocess.check_output(['node','-e',program,str(script)],text=True,encoding='utf-8'))
    assert all(a==b for a,b in rows)
    assert rows[0][0]=='linli-07'
    assert rows[1][0]=='linli-108'
    assert all(row[0]=='linli-01' for row in rows[2:])


def test_assets_are_exactly_108_real_transparent_pngs():
    import cv2
    root=Path('runtime/letter_stickers')
    files=list(root.glob('linli-*.png'))
    assert len(files)==108
    for file in files:
        im=cv2.imread(str(file),cv2.IMREAD_UNCHANGED)
        assert im.shape==(512,512,4)
        lo,hi=im[:,:,3].min(),im[:,:,3].max()
        assert lo==0 and hi>100
