from __future__ import annotations

import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_mailbox_entry_survives_skipping_setup_and_music_navigation() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    harness = r'''
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');
let source = fs.readFileSync(0, 'utf8').replace(/\s*schedule\(\);\s*\}\)\(\);\s*$/, `
 globalThis.navigation = { mountMainNavigation, finishInitialSetup };
})();`);
class Element {
 constructor(tag) { this.tagName=tag; this.children=[]; this.style={}; this.attributes={}; this.listeners={}; }
 setAttribute(k,v) { this.attributes[k]=v; }
 getAttribute(k) { return k==='href' ? this.href : this.attributes[k]; }
 removeAttribute(k) { delete this.attributes[k]; }
 append(...items) { items.forEach(i=>{i.parent=this;this.children.push(i)}); }
 remove() { if(this.parent) this.parent.children=this.parent.children.filter(x=>x!==this); }
 addEventListener(k,fn) { this.listeners[k]=fn; }
 querySelectorAll(selector) { return this.children.filter(x=>x.tagName==='a'); }
}
const body=new Element('body');
const document={body,documentElement:body,currentScript:{dataset:{apiBase:'http://127.0.0.1:8899'}},
 createElement:tag=>new Element(tag),
 querySelector:selector=>body.children.find(x=>Object.hasOwn(x.attributes,'data-olivia-main-navigation')) || null};
const window={location:{pathname:'/feapp/index.html',hash:'#/studio'},addEventListener(){},setTimeout,clearTimeout};
const calls=[];
const context={URL,AbortController,document,window,
 fetch:async(url,options)=>{calls.push({path:url.pathname,body:JSON.parse(options.body)});return {ok:true,json:async()=>({status:'READY'})}},
 MutationObserver:class{observe(){}}};
vm.runInNewContext(source,context);
(async()=>{
 context.navigation.mountMainNavigation();
 assert.equal(body.children.length,1);
 assert.equal(body.children[0].children[0].href,'#/collection');
 assert.equal(body.children[0].children[0].textContent,'信箱');
 await context.navigation.finishInitialSetup(true);
 assert.equal(window.location.hash,'#/collection');
 assert.deepEqual(calls,[{path:'/toy/setup/complete',body:{skipped:true}}]);
 context.navigation.mountMainNavigation();
 context.navigation.mountMainNavigation();
 assert.equal(body.children.length,1,'rerenders must not duplicate navigation');
 assert.equal(body.children[0].children[0].getAttribute('aria-current'),'page');
 window.location.hash='#/studio';
 context.navigation.mountMainNavigation();
 assert.equal(body.children[0].children[0].href,'#/collection','music must retain a mailbox entry');
 assert.equal(body.children[0].children[1].getAttribute('aria-current'),'page');
 window.location.hash='#/login';
 context.navigation.mountMainNavigation();
 assert.equal(body.children.length,0,'do not mount navigation on login');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, "-e", harness], input=BOOTSTRAP_JAVASCRIPT,
                            text=True, encoding="utf-8", capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
