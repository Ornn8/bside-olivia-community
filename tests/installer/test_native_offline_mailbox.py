import json
import subprocess

from patch_companion_settings import MAIN_JS_0627, MAILBOX_WRITE_ANCHOR_0627, _repair_mailbox_write_access


def test_offline_mailbox_loads_and_only_allows_local_letter_requests(tmp_path):
    main=tmp_path/MAIN_JS_0627
    main.parent.mkdir(parents=True)
    main.write_text(MAILBOX_WRITE_ANCHOR_0627+';Te.interceptors.request.use(e=>{const t=Ie();if(t.isOfflineMode)throw new Ol(e);return e});He(()=>{p.value||d.fetchMailList(!0)});',encoding='utf-8')
    _repair_mailbox_write_access(tmp_path)
    source=main.read_text(encoding='utf-8')
    assert 'p.value||d.fetchMailList' not in source
    helper=source[source.index('function oliviaLocalMailboxRequest'):source.index('Te.interceptors.request.use')]
    script='const document={querySelector:()=>({dataset:{apiBase:"http://127.0.0.1:18999/"}})};'+helper+'''
const check=(url,baseURL)=>oliviaLocalMailboxRequest({url,baseURL});
console.log(JSON.stringify([
check('/letter/list','http://127.0.0.1:18999/toy'),
check('/toy/letter/send','http://127.0.0.1:18999'),
check('/letter/list','https://example.com'),
check('http://127.0.0.1:8899/letter/list','http://127.0.0.1:18999'),
check('/letter/share','http://127.0.0.1:18999'),
check('/midi/create','http://127.0.0.1:18999')
]));
'''
    result=subprocess.run(['node','-e',script],check=True,capture_output=True,text=True)
    assert json.loads(result.stdout)==[True,True,False,False,False,False]
    assert _repair_mailbox_write_access(tmp_path)=='ALREADY_PATCHED'
