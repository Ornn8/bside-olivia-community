import shutil
import subprocess

import pytest

from runtime.call_ui import JAVASCRIPT

HARNESS = '''
globalThis.sampleRate = 48000; globalThis.currentFrame = 0;
class AudioWorkletProcessor { constructor(){ this.port = {postMessage: m => (globalThis.posted = globalThis.posted || []).push(m), onmessage: null}; } }
const registry = {}; globalThis.registerProcessor = (n, c) => registry[n] = c;
%s
const mic = new registry['olivia-mic']();
const input = new Float32Array(128).fill(0.5);
for (let i = 0; i < 1300; i++) mic.process([[input]]);
const frames = posted.filter(m => m instanceof ArrayBuffer);
if (frames.length !== 34 || frames[0].byteLength !== 3200) throw Error('mic frames ' + frames.length);
if (new Int16Array(frames[0])[10] !== Math.round(0.5 * 32767) && new Int16Array(frames[0])[10] !== Math.floor(0.5 * 32767)) throw Error('level');
mic.port.onmessage({data: {muted: true}}); posted.length = 0;
for (let i = 0; i < 50; i++) mic.process([[input]]);
if (new Int16Array(posted[0])[1599] !== 0) throw Error('mute');  // the frame's start was captured before muting
posted.length = 0;
const player = new registry['olivia-player']();
const out = [[new Float32Array(128)]];
player.port.onmessage({data: {turn: 1, samples: new Float32Array(200).fill(0.1)}});
player.process([], out); player.process([], out);   // turn 1 plays out before turn 2 arrives
player.port.onmessage({data: {turn: 2, samples: new Float32Array(100).fill(0.2)}});
player.process([], out);
const drained = posted.filter(m => m.drained !== undefined).map(m => m.drained);
if (JSON.stringify(drained) !== '[1,2]') throw Error('drained ' + JSON.stringify(drained));
player.port.onmessage({data: {turn: 3, samples: new Float32Array(500)}}); player.port.onmessage({data: {clear: true}});
if (player.queue.length) throw Error('clear');
// A newer answer drops what is left of older ones, and late frames of an older answer are not played.
player.port.onmessage({data: {turn: 4, samples: new Float32Array(500).fill(0.4)}});
player.process([], out);
player.port.onmessage({data: {turn: 5, samples: new Float32Array(300).fill(0.5)}});
if (player.queue.length !== 1 || player.queue[0].turn !== 5 || player.head !== 0) throw Error('newer turn');
player.port.onmessage({data: {turn: 4, samples: new Float32Array(300).fill(0.4)}});
if (player.queue.length !== 1) throw Error('late older frames');
player.process([], out);
if (Math.abs(out[0][0] - 0.5) > 1e-6) throw Error('plays the newer turn');
console.log('WORKLETS_OK');
'''


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not installed')
def test_microphone_framing_mute_and_player_queue(tmp_path):
    start = JAVASCRIPT.index('const WORKLETS = `') + len('const WORKLETS = `')
    worklets = JAVASCRIPT[start:JAVASCRIPT.index('`;', start)]
    script = tmp_path / 'worklets.js'
    script.write_text(HARNESS % worklets, encoding='utf-8')
    result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=60)
    assert result.stdout.strip() == 'WORKLETS_OK', result.stderr


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not installed')
def test_call_page_script_parses(tmp_path):
    script = tmp_path / 'call.js'
    script.write_text(JAVASCRIPT, encoding='utf-8')
    result = subprocess.run(['node', '--check', str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
