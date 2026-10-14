"""The call page: a phone button and a call overlay in the native client.

Microphone: 16 kHz PCM16 in 100 ms frames, with the webview's echo cancellation, noise
suppression and gain control. Her voice: 24 kHz PCM16 frames prefixed by a 4-byte turn number,
played through an AudioWorklet queue that is dropped at once when the server says "stop".
"""

JAVASCRIPT = r'''
(() => {
  if (typeof document === 'undefined' || !document.head || typeof document.querySelector !== 'function'
      || typeof window === 'undefined' || window.__oliviaCallUi) return;
  window.__oliviaCallUi = true;
  const css = document.createElement('style');
  css.textContent = `
  .olivia-call-fab{position:fixed;right:22px;bottom:22px;z-index:9000;width:52px;height:52px;border-radius:50%;border:0;
    background:#a48c5c;color:#fff;box-shadow:0 6px 18px #0004;cursor:pointer;display:none;align-items:center;justify-content:center}
  .olivia-call-fab[data-ready]{display:flex}.olivia-call-fab svg{width:24px;height:24px;fill:currentColor}
  .olivia-call-fab:focus-visible{outline:3px solid #f3eee4;outline-offset:3px}
  .olivia-call{position:fixed;inset:0;z-index:9500;background:#0009;display:flex;align-items:center;justify-content:center;
    font:14px/1.6 "Microsoft YaHei",system-ui,sans-serif}
  .olivia-call-card{width:min(360px,92vw);padding:28px 24px;border-radius:20px;background:#1f1d1b;color:#eee9df;text-align:center;
    box-shadow:0 16px 48px #0008}
  .olivia-call-orb{width:96px;height:96px;margin:6px auto 18px;border-radius:50%;background:radial-gradient(circle at 35% 35%,#d9c39a,#7c6640);
    transition:transform .12s ease-out}
  .olivia-call-card[data-state="ringing"] .olivia-call-orb{animation:olivia-ring 1.6s ease-in-out infinite}
  @keyframes olivia-ring{0%,100%{transform:scale(1)}50%{transform:scale(1.08)}}
  .olivia-call-title{font-size:18px;margin:0}.olivia-call-status{min-height:22px;color:#c9bfae;margin:6px 0 14px;font-variant-numeric:tabular-nums}
  .olivia-call-price{font-size:12px;color:#a69c8b;margin:0 0 18px}
  .olivia-call-caption{min-height:44px;max-height:88px;overflow:hidden;font-size:13px;color:#d8cfbf;margin:0 0 18px}
  .olivia-call-actions{display:flex;gap:14px;justify-content:center}
  .olivia-call-actions button{min-width:88px;padding:10px 14px;border-radius:22px;border:1px solid #5a554d;background:#2c2a27;color:#eee9df;cursor:pointer}
  .olivia-call-actions button[data-kind="dial"]{background:#3f8f5a;border-color:#3f8f5a}
  .olivia-call-actions button[data-kind="hangup"]{background:#b6463c;border-color:#b6463c}
  .olivia-call-actions button[aria-pressed="true"]{background:#eee9df;color:#1f1d1b}
  .olivia-call-actions button:disabled{opacity:.5;cursor:default}`;
  document.head.append(css);

  const WORKLETS = `
  class Mic extends AudioWorkletProcessor{
    constructor(){super();this.ratio=sampleRate/16000;this.pos=0;this.acc=0;this.n=0;this.out=new Int16Array(1600);this.fill=0;this.muted=false;
      this.port.onmessage=e=>{this.muted=!!e.data.muted}}
    process(inputs){const input=inputs[0]&&inputs[0][0];if(!input)return true;
      for(let i=0;i<input.length;i++){this.acc+=input[i];this.n++;this.pos+=1;
        if(this.pos>=this.ratio){this.pos-=this.ratio;let v=this.muted?0:this.acc/this.n;this.acc=0;this.n=0;
          v=Math.max(-1,Math.min(1,v));this.out[this.fill++]=v<0?v*32768:v*32767;
          if(this.fill===this.out.length){this.port.postMessage(this.out.buffer.slice(0));this.fill=0}}}
      return true}}
  class Player extends AudioWorkletProcessor{
    constructor(){super();this.queue=[];this.head=0;this.turn=0;this.level=0;
      this.port.onmessage=e=>{const d=e.data;if(d.clear){this.queue=[];this.head=0;return}
        if(d.turn>this.turn){this.turn=d.turn;
          if(this.queue.length&&this.queue[0].turn<d.turn)this.head=0;
          this.queue=this.queue.filter(c=>c.turn>=d.turn)}
        if(d.turn<this.turn)return;
        this.queue.push(d)}}
    process(_,outputs){const out=outputs[0][0];let sum=0;
      for(let i=0;i<out.length;i++){
        while(this.queue.length&&this.head>=this.queue[0].samples.length){const done=this.queue.shift();this.head=0;
          if(!this.queue.length||this.queue[0].turn!==done.turn)this.port.postMessage({drained:done.turn})}
        if(!this.queue.length){out[i]=0;continue}
        const chunk=this.queue[0];out[i]=chunk.samples[this.head++];sum+=out[i]*out[i]}
      this.level=Math.sqrt(sum/out.length);if(currentFrame%4096<128)this.port.postMessage({level:this.level});
      return true}}
  registerProcessor('olivia-mic',Mic);registerProcessor('olivia-player',Player);`;

  const PHONE = 'M6.6 10.8a15.1 15.1 0 0 0 6.6 6.6l2.2-2.2a1 1 0 0 1 1-.25 11.4 11.4 0 0 0 3.6.57 1 1 0 0 1 1 1V20a1 1 0 0 1-1 1A17 17 0 0 1 3 4a1 1 0 0 1 1-1h3.5a1 1 0 0 1 1 1c0 1.25.2 2.45.57 3.6a1 1 0 0 1-.25 1z';
  const icon = () => { const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true'); const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', PHONE); svg.append(path); return svg; };
  let base = null, config = null, overlay = null;
  const yuan = cents => '¥' + (cents / 100).toFixed(2);
  const node = (tag, cls, text, parent) => { const el = document.createElement(tag); if (cls) el.className = cls; if (text) el.textContent = text; if (parent) parent.append(el); return el; };

  async function local(path, body) {
    const response = await fetch(new URL(path, base), body === undefined ? {} : {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Olivia-Companion-Action': 'confirmed'}, body: JSON.stringify(body)});
    const value = await response.json().catch(() => ({}));
    if (!response.ok || (value.code && value.code !== 200)) throw Error(value?.data?.error_code || value?.msg || 'CALL_UNAVAILABLE');
    return value.data;
  }

  const fab = node('button', 'olivia-call-fab');
  fab.type = 'button'; fab.append(icon()); fab.setAttribute('aria-label', '给她打电话'); fab.title = '给她打电话';
  fab.onclick = () => open();

  async function refreshConfig() {
    try { config = await local('/toy/call/config'); } catch (_) { config = null; }
    fab.toggleAttribute('data-ready', !!config?.available);
  }

  function priceText() {
    if (!config?.cents_per_minute) return '';
    const low = config.base_cents_per_minute, high = config.busy_cents_per_minute;
    return high && low && high !== low
      ? `每分钟 ${yuan(low)}，通话的人多时 ${yuan(high)}；接通时的价格整通电话不变，按秒计费。`
      : `每分钟 ${yuan(config.cents_per_minute)}，按秒计费。`;
  }

  const MESSAGES = {
    CALL_ACCOUNT_REQUIRED: '还没有连接 Olivia 账户，暂时不能打电话。',
    CALL_INSUFFICIENT_BALANCE: '余额不足，充值后再打吧。', CALL_BUSY: '线路有点忙，稍后再打试试。',
    CALL_NETWORK: '网络不太稳定，没能打通。', CALL_AUTH_FAILED: '账户 Key 无效，请在设置里重新连接。',
    MIC: '没能打开麦克风，请检查麦克风权限和设备。'
  };

  function open() {
    if (overlay) return;
    overlay = node('div', 'olivia-call', null, document.body);
    overlay.setAttribute('role', 'dialog'); overlay.setAttribute('aria-modal', 'true'); overlay.setAttribute('aria-label', '语音通话');
    const card = node('div', 'olivia-call-card', null, overlay); card.dataset.state = 'idle';
    const orb = node('div', 'olivia-call-orb', null, card);
    node('p', 'olivia-call-title', '语音通话', card);
    const status = node('p', 'olivia-call-status', '', card); status.setAttribute('role', 'status');
    const price = node('p', 'olivia-call-price', priceText(), card);
    const caption = node('p', 'olivia-call-caption', '', card); caption.setAttribute('aria-live', 'polite');
    const actions = node('div', 'olivia-call-actions', null, card);
    const dial = node('button', null, '拨打', actions); dial.type = 'button'; dial.dataset.kind = 'dial';
    const mute = node('button', null, '静音', actions); mute.type = 'button'; mute.hidden = true; mute.setAttribute('aria-pressed', 'false');
    const hang = node('button', null, '取消', actions); hang.type = 'button'; hang.dataset.kind = 'hangup';
    let call = null;
    hang.onclick = () => call ? call.hangup() : close();
    mute.onclick = () => { const on = mute.getAttribute('aria-pressed') !== 'true'; mute.setAttribute('aria-pressed', String(on)); call?.mute(on); };
    dial.onclick = async () => {
      dial.hidden = true; hang.textContent = '挂断'; card.dataset.state = 'ringing'; status.textContent = '正在呼叫…';
      call = new Call({status, caption, orb, card, mute, hang, price, done: () => { call = null; }});
      await call.start();
    };
    hang.focus();
  }

  function close() { overlay?.remove(); overlay = null; }

  class Call {
    constructor(ui) {
      this.ui = ui; this.transcript = []; this.turn = 0; this.stopped = new Set(); this.ended = false; this.her = '';
      this.herDone = new Set(); this.lastAudioAt = {}; this.drainedAt = {};
    }

    async start() {
      const ui = this.ui;
      try {
        this.stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true,
          autoGainControl: true, channelCount: 1}});
      } catch (_) { return this.fail('MIC'); }
      this.context = new AudioContext();
      await this.context.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLETS], {type: 'text/javascript'})));
      this.player = new AudioWorkletNode(this.context, 'olivia-player', {outputChannelCount: [1]});
      this.player.connect(this.context.destination);
      this.player.port.onmessage = e => this.fromPlayer(e.data);
      this.ringing = this.ring();
      const minimum = new Promise(r => setTimeout(r, 2500));
      let started;
      try { started = await local('/toy/call/start', {}); } catch (e) { await minimum; return this.fail(e.message); }
      if (started.pickup === 'missed') {
        await new Promise(r => setTimeout(r, 18000));
        return this.finish('暂时无人接听', null);
      }
      if (started.cents_per_minute) ui.price.textContent = `本次通话每分钟 ${yuan(started.cents_per_minute)}，按秒计费。`;
      await minimum;
      this.connect(started);
    }

    ring() {
      const gain = this.context.createGain(); gain.gain.value = 0; gain.connect(this.context.destination);
      const tone = this.context.createOscillator(); tone.frequency.value = 425; tone.connect(gain); tone.start();
      const t0 = this.context.currentTime;
      for (let i = 0; i < 15; i++) { gain.gain.setValueAtTime(0.08, t0 + i * 5); gain.gain.setValueAtTime(0, t0 + i * 5 + 1); }
      return () => { try { tone.stop(); gain.disconnect(); } catch (_) {} };
    }

    connect(started) {
      const ws = this.ws = new WebSocket(started.ws_url);
      ws.binaryType = 'arraybuffer';
      this.pickup = started.pickup;
      ws.onopen = () => ws.send(JSON.stringify({type: 'start', v: 1, ticket: started.ticket}));
      ws.onmessage = e => typeof e.data === 'string' ? this.message(JSON.parse(e.data)) : this.audio(e.data);
      ws.onclose = () => { if (!this.ended) this.finish(this.startedAt ? '通话已结束' : '没能接通，请稍后再试', null); };
    }

    message(m) {
      const ui = this.ui;
      if (m.type === 'ready') {
        this.ringing?.(); this.ringing = null; this.startedAt = Date.now(); ui.card.dataset.state = 'talking';
        ui.mute.hidden = false; this.clock = setInterval(() => this.tick(), 500); this.tick();
        this.capture(); this.worldTimer = setInterval(() => this.world(), 300000);
      } else if (m.type === 'her') {
        if (m.turn !== this.turn) { this.turn = m.turn; this.her = ''; }
        this.her += m.delta; ui.caption.textContent = this.her;
      } else if (m.type === 'stop') {
        this.stopped.add(m.turn); this.player.port.postMessage({clear: true});
      } else if (m.type === 'her_done') {
        this.herDone.add(m.turn);
        if ((this.drainedAt[m.turn] || 0) > (this.lastAudioAt[m.turn] || 0)) this.reportPlayed(m.turn);
      } else if (m.type === 'notice' && m.reason === 'limit') {
        ui.status.textContent = '余额快用完了，通话即将结束';
      } else if (m.type === 'end') {
        this.report = m; this.finish('通话结束', m);
      }
    }

    audio(buffer) {
      const view = new DataView(buffer), turn = view.getUint32(0);
      if (this.stopped.has(turn)) return;
      this.lastAudioAt[turn] = performance.now();
      const pcm = new Int16Array(buffer, 4), rate = this.context.sampleRate, ratio = 24000 / rate;
      const samples = new Float32Array(Math.floor(pcm.length / ratio));
      for (let i = 0; i < samples.length; i++) {
        const x = i * ratio, j = Math.floor(x), f = x - j, a = pcm[j] || 0, b = pcm[Math.min(j + 1, pcm.length - 1)] || 0;
        samples[i] = (a + (b - a) * f) / 32768;
      }
      this.player.port.postMessage({turn, samples}, [samples.buffer]);
    }

    fromPlayer(d) {
      if (d.level !== undefined) { this.ui.orb.style.transform = `scale(${1 + Math.min(0.25, d.level * 3)})`; return; }
      if (d.drained === undefined) return;
      // The player can run dry between two segments; only the end after the server's last frame counts.
      this.drainedAt[d.drained] = performance.now();
      if (this.herDone.has(d.drained)) this.reportPlayed(d.drained);
    }

    reportPlayed(turn) { if (this.ws?.readyState === 1) this.ws.send(JSON.stringify({type: 'played', turn})); }

    async capture() {
      const source = this.context.createMediaStreamSource(this.stream);
      this.mic = new AudioWorkletNode(this.context, 'olivia-mic');
      this.mic.port.onmessage = e => { if (this.ws?.readyState === 1) this.ws.send(e.data); };
      const sink = this.context.createGain(); sink.gain.value = 0;
      source.connect(this.mic); this.mic.connect(sink); sink.connect(this.context.destination);
    }

    mute(on) { this.mic?.port.postMessage({muted: on}); }

    async world() {
      try {
        const value = await local('/toy/call/world');
        const text = JSON.stringify(value.world);
        if (text !== this.lastWorld && this.ws?.readyState === 1) { this.lastWorld = text; this.ws.send(JSON.stringify({type: 'world', world: value.world})); }
      } catch (_) {}
    }

    tick() {
      const s = Math.floor((Date.now() - this.startedAt) / 1000);
      this.ui.status.textContent = '通话中 ' + String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
    }

    hangup() {
      if (this.ws?.readyState === 1) {
        this.ui.status.textContent = '正在挂断…'; this.ws.send(JSON.stringify({type: 'hangup'}));
        this.hangTimer = setTimeout(() => this.finish('通话结束', null), 20000);
      } else this.finish('已取消', null);
    }

    fail(code) { this.finish(MESSAGES[code] || '暂时没能打通，请稍后再试', null); }

    finish(text, report) {
      if (this.ended) return; this.ended = true;
      clearInterval(this.clock); clearInterval(this.worldTimer); clearTimeout(this.hangTimer);
      this.ringing?.(); this.stream?.getTracks().forEach(t => t.stop());
      try { this.ws?.close(); } catch (_) {}
      setTimeout(() => this.context?.close().catch(() => {}), 300);
      const ui = this.ui; ui.card.dataset.state = 'ended'; ui.mute.hidden = true;
      ui.status.textContent = text + (report?.seconds ? '  ' + Math.floor(report.seconds / 60) + ' 分 ' + (report.seconds % 60) + ' 秒' : '');
      if (report && typeof report.charge_cents === 'number') ui.price.textContent = '本次通话 ' + yuan(report.charge_cents);
      ui.hang.textContent = '关闭'; ui.hang.onclick = close;
      if (report?.transcript?.length && report.call_id) {
        local('/toy/call/end', {call_id: report.call_id, seconds: report.seconds, charge_cents: report.charge_cents,
          reason: report.reason, pickup: this.pickup, transcript: report.transcript, notes: report.notes}).catch(() => {});
      }
      ui.done();
    }
  }

  function boot(tries = 0) {
    // The page script runs from <head>: <body> does not exist yet (2.3.4+call.1-3 threw here and
    // took the whole settings page down with it).
    if (!document.body) { document.addEventListener('DOMContentLoaded', () => boot(tries), {once: true}); return; }
    const marker = document.querySelector('[data-olivia-companion-settings]');
    base = marker && marker.getAttribute('data-api-base');
    if (!base) { if (tries < 60) setTimeout(() => boot(tries + 1), 1000); return; }
    document.body.append(fab);
    refreshConfig(); setInterval(refreshConfig, 300000);
  }
  boot();
})();
'''
