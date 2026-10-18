"""The call page: a phone button and a call overlay in the native client.

Microphone: 16 kHz PCM16 in 40 ms frames (the end of speech is noticed sooner), with the webview's
echo cancellation, noise suppression and gain control. Her voice: 24 kHz PCM16 frames prefixed by a 4-byte turn number,
played through an AudioWorklet queue that is dropped at once when the server says "stop".
"用手机打" pairs a phone with a QR code: the phone page (bside-moon.cn/call/) dials, this computer builds
the call (runtime/call/desk.py).
"""
from runtime.call.qr import JAVASCRIPT as QR_JAVASCRIPT
from runtime.call_avatar_ui import JAVASCRIPT as AVATAR_JAVASCRIPT

JAVASCRIPT = r'''
(() => {
  if (typeof document === 'undefined' || !document.head || typeof document.querySelector !== 'function'
      || typeof window === 'undefined' || window.__oliviaCallUi) return;
  window.__oliviaCallUi = true;
  const t = (zh, vars) => window.oliviaI18n ? window.oliviaI18n.t(zh, vars) : String(zh).replace(/\{(\w+)\}/g, (m, k) => vars && k in vars ? vars[k] : m);
  const css = document.createElement('style');
  css.textContent = `
  .olivia-call-entry{display:none;align-items:center;gap:6px;height:32px;padding:0 14px 0 11px;border:1px solid #8c7a5c;
    border-radius:999px;background:#2b2621;color:#f3e7d7;font:inherit;font-size:14px;font-weight:600;line-height:20px;
    white-space:nowrap;cursor:pointer;-webkit-app-region:no-drag;transition:background .15s,border-color .15s}
  .olivia-call-entry[data-ready]{display:inline-flex}
  .olivia-call-entry:hover{background:#3a3229;border-color:#b39c72}
  .olivia-call-entry svg{width:15px;height:15px;fill:#d9c39a}
  .olivia-call-entry:focus-visible{outline:2px solid #f3eee4;outline-offset:2px}
  .olivia-call{position:fixed;inset:0;z-index:9500;background:rgba(8,8,10,.62);display:flex;align-items:center;
    justify-content:center;font-family:inherit;font-size:14px;line-height:1.6}
  .olivia-call-card{width:min(380px,92vw);padding:28px 24px 24px;border-radius:16px;background:#18191c;border:1px solid #2c2d31;
    color:#f2f0ec;text-align:center;box-shadow:0 24px 64px rgba(0,0,0,.55)}
  .olivia-call-orb{width:88px;height:88px;margin:4px auto 16px;border-radius:50%;
    background:radial-gradient(circle at 35% 30%,#efe3cc,#8e7b58 70%,#5e5039);box-shadow:0 0 0 6px rgba(221,214,201,.06);
    transition:transform .12s ease-out}
  .olivia-call-card[data-state="ringing"] .olivia-call-orb{animation:olivia-ring 1.6s ease-in-out infinite}
  .olivia-call-avatar{display:block;width:100%;aspect-ratio:1;margin:-14px auto 2px}
  .olivia-call-avatar[hidden]{display:none}
  .olivia-call-card.has-avatar .olivia-call-orb{display:none}
  .olivia-call-offer{display:flex;align-items:center;gap:10px;margin:-6px 0 14px;padding:10px 12px;border-radius:12px;
    background:rgba(255,255,255,.05);text-align:left;font-size:12px;color:#b9bbc0;line-height:1.5}
  .olivia-call-offer b{display:block;color:#f6f4f0;font-size:13px}
  .olivia-call-offer button{flex:none;margin-left:auto;padding:6px 12px;border:0;border-radius:999px;
    background:#efe3cc;color:#2a2620;font-weight:600;cursor:pointer}
  .olivia-call-offer button:disabled{opacity:.6;cursor:default}
  .olivia-call-card.has-avatar .olivia-call-offer{display:none}
  @keyframes olivia-ring{0%,100%{transform:scale(1)}50%{transform:scale(1.08)}}
  .olivia-call-title{font-size:20px;font-weight:700;margin:0;color:#f6f4f0}
  .olivia-call-status{min-height:22px;color:#b9bbc0;margin:4px 0 14px;font-variant-numeric:tabular-nums}
  .olivia-call-price{font-size:12px;color:#8b8d92;margin:0 0 18px}
  .olivia-call-models{display:flex;gap:8px;justify-content:center;margin:0 0 10px}
  .olivia-call-models button{padding:6px 14px;border-radius:999px;border:1px solid rgba(255,255,255,.28);background:transparent;
    color:#e3e1dc;cursor:pointer;font:inherit;font-size:13px;font-weight:600}
  .olivia-call-models button:hover{background:rgba(255,255,255,.06)}
  .olivia-call-models button[aria-pressed="true"]{background:#ddd6c9;color:#18191c;border-color:#ddd6c9}
  .olivia-call-models button:focus-visible{outline:2px solid #f3eee4;outline-offset:2px}
  .olivia-call-caption{min-height:44px;max-height:88px;overflow:hidden;font-size:13px;color:#d6d4cf;margin:0 0 18px}
  .olivia-call-actions{display:flex;gap:12px;justify-content:center}
  .olivia-call-actions button{min-width:96px;height:40px;padding:0 18px;border-radius:999px;border:1px solid rgba(255,255,255,.28);
    background:transparent;color:#f2f0ec;font:inherit;font-weight:600;cursor:pointer}
  .olivia-call-actions button:hover{background:rgba(255,255,255,.06)}
  .olivia-call-actions button:focus-visible{outline:2px solid #f3eee4;outline-offset:2px}
  .olivia-call-actions button[data-kind="dial"]{background:#ddd6c9;border-color:#ddd6c9;color:#18191c}
  .olivia-call-actions button[data-kind="hangup"]{background:#c0473d;border-color:#c0473d;color:#fff}
  .olivia-call-actions button[aria-pressed="true"]{background:#ddd6c9;border-color:#ddd6c9;color:#18191c}
  .olivia-call-actions button:disabled{opacity:.5;cursor:default}
  .olivia-call-phone-link{margin:16px 0 0;padding:4px 6px;border:0;background:none;color:#a9a294;font:inherit;font-size:13px;
    text-decoration:underline;text-underline-offset:3px;cursor:pointer}
  .olivia-call-phone-link:hover{color:#ddd6c9}
  .olivia-call-phone-link:focus-visible{outline:2px solid #f3eee4;outline-offset:2px}
  .olivia-call-phone{display:grid;gap:12px;justify-items:center}
  .olivia-call-phone p{margin:0;color:#b9bbc0;font-size:13px}
  .olivia-call-qr{width:200px;height:200px;padding:12px;border-radius:12px;background:#fff;display:flex;align-items:center;justify-content:center}
  .olivia-call-qr svg{width:100%;height:100%;display:block}
  .olivia-call-phones{width:100%;display:grid;gap:6px;margin:4px 0 0;padding:0;list-style:none;text-align:left}
  .olivia-call-phones li{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 12px;border-radius:10px;
    background:#202125;color:#e3e1dc;font-size:13px}
  .olivia-call-phones button{border:0;background:none;color:#a9a294;font:inherit;font-size:12px;cursor:pointer;text-decoration:underline}`;
  document.head.append(css);

  const WORKLETS = `
  class Mic extends AudioWorkletProcessor{
    constructor(){super();this.ratio=sampleRate/16000;this.pos=0;this.acc=0;this.n=0;this.out=new Int16Array(640);this.fill=0;this.muted=false;
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

/*QR*/
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

  // The phone button sits in the header's account group (next to 账户), styled like the header's pills.
  const entry = node('button', 'olivia-call-entry');
  entry.type = 'button'; entry.append(icon()); node('span', null, t('打电话'), entry);
  entry.setAttribute('aria-label', t('给她打电话')); entry.title = t('给她打电话');
  entry.onclick = () => open();
  let placing = false;
  function place() {
    placing = false;
    const group = document.querySelector('[data-olivia-service-buttons]');
    if (group && entry.parentElement !== group) group.prepend(entry);
  }

  // A slow or failed read keeps the last answer and tries again in 30 s: one network hiccup to the
  // call service used to hide the button for five minutes (2026-10-14). Only a real answer from the
  // service (open, closed, no account) changes it.
  let configRetry = null;
  async function refreshConfig() {
    let value = null;
    try { value = await local('/toy/call/config'); } catch (_) {}
    clearTimeout(configRetry);
    if (!value || value.reason === 'network' || value.reason === 'service') {
      configRetry = setTimeout(refreshConfig, 30000);
      if (config || !value) return;
    }
    config = value;
    entry.toggleAttribute('data-ready', !!config?.available);
  }

  // The user picks the model of each call; the last choice is remembered on this computer.
  const MODEL_LABELS = {flash: [t('标准'), t('日常聊天')], plus: [t('增强'), t('回应更生动、更像她')]};
  const MODEL_KEY = 'olivia-call-model';
  const models = () => (config?.models || []).filter(m => MODEL_LABELS[m.id] && typeof m.cents_per_minute === 'number');
  function chosenModel() {
    let saved = null;
    try { saved = localStorage.getItem(MODEL_KEY); } catch (_) {}
    const list = models();
    return (list.find(m => m.id === saved) || list.find(m => m.id === config?.default_model) || list[0] || null);
  }
  function remember(id) { try { localStorage.setItem(MODEL_KEY, id); } catch (_) {} }

  function priceText(model) {
    const plan = model || (config?.cents_per_minute ? config : null);
    if (!plan?.cents_per_minute) return '';
    const low = plan.base_cents_per_minute, high = plan.busy_cents_per_minute;
    return high && low && high !== low
      ? t('每分钟 {low}，通话的人多时 {high}；接通时的价格整通电话不变，按秒计费。', {low: yuan(low), high: yuan(high)})
      : t('每分钟 {price}，按秒计费。', {price: yuan(plan.cents_per_minute)});
  }

  const MESSAGES = {
    CALL_ACCOUNT_REQUIRED: t('还没有连接 Olivia 账户，暂时不能打电话。'),
    CALL_INSUFFICIENT_BALANCE: t('余额不足，充值后再打吧。'), CALL_BUSY: t('线路有点忙，稍后再打试试。'),
    CALL_NETWORK: t('网络不太稳定，没能打通。'), CALL_AUTH_FAILED: t('账户 Key 无效，请在设置里重新连接。'),
    CALL_MODEL_UNKNOWN: t('这个通话模型暂时不能用，换一个试试。'),
    MIC: t('没能打开麦克风，请检查麦克风权限和设备。')
  };

  // Her avatar, for accounts that own it, takes the orb's place (files come through this computer's backend).
  let avatar = null, avatarState = null;
  async function showAvatar(canvas, card, call) {
    if (!window.OliviaAvatar) return;
    try {
      if (!avatarState?.owned) avatarState = await local('/toy/call/avatar');
      if (!avatarState.owned && avatarState.for_sale && avatarState.available && Number.isInteger(avatarState.cents)) {
        offerAvatar(canvas, card, call);
        return;
      }
      if (!avatarState.owned || !avatarState.available) return;
      const made = await window.OliviaAvatar.create(canvas, name => fetch(new URL('/toy/call/avatar/files/' + name, base)));
      if (!made) return;
      if (!canvas.isConnected) { made.destroy(); return; }
      avatar = made; canvas.hidden = false; card.classList.add('has-avatar');
      call()?.attach(made);
    } catch (_) {}
  }

  // Not owned yet: one line under the orb to unlock her avatar (the price is confirmed before buying).
  function offerAvatar(canvas, card, call) {
    if (card.querySelector('.olivia-call-offer')) return;
    const price = '¥' + (avatarState.cents / 100).toFixed(avatarState.cents % 100 ? 2 : 0);
    const offer = node('div', 'olivia-call-offer');
    const words = node('div', null, null, offer);
    node('b', null, t('她的通话形象'), words);
    const note = node('span', null, t('通话时换成会动的她，表情和手势跟着说话变化。'), words);
    const buy = node('button', null, t('解锁 {price}', {price}), offer);
    buy.type = 'button';
    // Two steps in the button itself: the first click states the one-time price, the second buys.
    buy.onclick = async () => {
      if (!buy.dataset.armed) {
        buy.dataset.armed = '1'; buy.textContent = t('确认 {price}', {price});
        note.textContent = t('一次性 {price}，之后一直可用。再点一次确认购买。', {price});
        return;
      }
      buy.disabled = true;
      try {
        const result = await local('/toy/call/avatar/buy', {max_cents: avatarState.cents});
        if (result.owned) {
          avatarState = result; offer.remove();
          await showAvatar(canvas, card, call);
          return;
        }
        note.textContent = result.error === 'balance' ? t('余额不足，先到「账户」里充值再来解锁吧。') : t('暂时没能完成购买，请稍后再试。');
      } catch (_) {
        note.textContent = t('暂时没能完成购买，请稍后再试。');
      }
      delete buy.dataset.armed; buy.textContent = t('解锁 {price}', {price}); buy.disabled = false;
    };
    card.querySelector('.olivia-call-orb')?.after(offer);
  }

  function open() {
    if (overlay) return;
    overlay = node('div', 'olivia-call', null, document.body);
    overlay.setAttribute('role', 'dialog'); overlay.setAttribute('aria-modal', 'true'); overlay.setAttribute('aria-label', t('语音通话'));
    const card = node('div', 'olivia-call-card', null, overlay); card.dataset.state = 'idle';
    const face = node('canvas', 'olivia-call-avatar', null, card); face.hidden = true; face.setAttribute('aria-hidden', 'true');
    const orb = node('div', 'olivia-call-orb', null, card);
    node('p', 'olivia-call-title', t('语音通话'), card);
    const status = node('p', 'olivia-call-status', '', card); status.setAttribute('role', 'status');
    let model = chosenModel();
    const choices = node('div', 'olivia-call-models', null, card);
    choices.setAttribute('role', 'group'); choices.setAttribute('aria-label', t('通话模型'));
    choices.hidden = models().length < 2;
    const price = node('p', 'olivia-call-price', priceText(model), card);
    for (const plan of models()) {
      const [label, hint] = MODEL_LABELS[plan.id];
      const button = node('button', null, t('{label} {price}/分钟', {label, price: yuan(plan.cents_per_minute)}), choices);
      button.type = 'button'; button.title = hint; button.dataset.model = plan.id;
      button.setAttribute('aria-pressed', String(plan.id === model?.id));
      button.onclick = () => {
        model = plan; remember(plan.id); price.textContent = priceText(plan);
        for (const other of choices.children) other.setAttribute('aria-pressed', String(other === button));
      };
    }
    const caption = node('p', 'olivia-call-caption', '', card); caption.setAttribute('aria-live', 'polite');
    const actions = node('div', 'olivia-call-actions', null, card);
    const dial = node('button', null, t('拨打'), actions); dial.type = 'button'; dial.dataset.kind = 'dial';
    const mute = node('button', null, t('静音'), actions); mute.type = 'button'; mute.hidden = true; mute.setAttribute('aria-pressed', 'false');
    const hang = node('button', null, t('取消'), actions); hang.type = 'button'; hang.dataset.kind = 'cancel';
    const toPhone = node('button', 'olivia-call-phone-link', t('用手机打'), card); toPhone.type = 'button';
    toPhone.hidden = !config?.phone;
    toPhone.onclick = () => phonePanel(card);
    let call = null;
    showAvatar(face, card, () => call);
    hang.onclick = () => call ? call.hangup() : close();
    mute.onclick = () => { const on = mute.getAttribute('aria-pressed') !== 'true'; mute.setAttribute('aria-pressed', String(on)); call?.mute(on); };
    dial.onclick = async () => {
      dial.hidden = true; choices.hidden = true; toPhone.hidden = true; hang.textContent = t('挂断'); hang.dataset.kind = 'hangup'; card.dataset.state = 'ringing'; status.textContent = t('正在呼叫…');
      call = new Call({status, caption, orb, card, mute, hang, price, model: model?.id, done: () => { call = null; }});
      if (avatar) call.attach(avatar);
      await call.start();
    };
    dial.focus();
  }

  function close() { clearInterval(phoneTimer); avatar?.destroy(); avatar = null; overlay?.remove(); overlay = null; }

  // 用手机打: a one-time QR link pairs the phone; afterwards the phone page calls her while this
  // computer builds each call. The list shows the paired phones and lets the user unpair them.
  let phoneTimer = null;
  function qrSvg(text) {
    const grid = qrMatrix(text), n = grid.length + 8;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', `0 0 ${n} ${n}`); svg.setAttribute('shape-rendering', 'crispEdges');
    svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', t('配对二维码'));
    let d = '';
    grid.forEach((row, y) => row.forEach((dark, x) => { if (dark) d += `M${x + 4} ${y + 4}h1v1h-1z`; }));
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d); path.setAttribute('fill', '#111'); svg.append(path);
    return svg;
  }
  function phonePanel(card) {
    const shown = [...card.children];
    shown.forEach(el => { el.dataset.wasHidden = el.hidden ? '1' : ''; el.hidden = true; });
    const panel = node('div', 'olivia-call-phone', null, card);
    node('p', 'olivia-call-title', t('用手机打'), panel);
    const intro = node('p', null, t('用手机相机扫这个二维码，打开的页面就能给她打电话。电脑上的 Olivia 需要开着，她的记忆和今天的生活都从这里读取。'), panel);
    const box = node('div', 'olivia-call-qr', null, panel);
    const hint = node('p', null, t('正在生成二维码…'), panel); hint.setAttribute('role', 'status');
    const list = node('ul', 'olivia-call-phones', null, panel);
    const back = node('button', null, t('返回'), node('div', 'olivia-call-actions', null, panel)); back.type = 'button';
    let known = null;
    const draw = phones => {
      list.replaceChildren();
      for (const phone of phones) {
        const li = node('li', null, null, list);
        const when = phone.created ? new Date(phone.created * 1000).toLocaleDateString(t('zh-CN'), {month: 'long', day: 'numeric'}) : '';
        node('span', null, `${phone.label || t('手机')}${when ? t('（{when}配对）', {when}) : ''}`, li);
        const off = node('button', null, t('解除配对'), li); off.type = 'button';
        off.onclick = async () => {
          off.disabled = true;
          try { await local('/toy/call/phone/unpair', {id: phone.id}); await refresh(); } catch (_) { off.disabled = false; }
        };
      }
    };
    const refresh = async () => {
      if (!overlay || !panel.isConnected) return clearInterval(phoneTimer);
      let value;
      try { value = await local('/toy/call/phones'); } catch (_) { return; }
      const phones = value.phones || [];
      if (known !== null && phones.length > known) { hint.textContent = t('配对成功，现在可以在手机上给她打电话了。'); box.hidden = true; intro.hidden = true; }
      known = phones.length; draw(phones);
    };
    const retry = node('button', null, t('重试'), node('div', 'olivia-call-actions', null, panel)); retry.type = 'button';
    retry.parentElement.hidden = true;
    const pair = async () => {
      retry.parentElement.hidden = true; box.hidden = false; hint.textContent = t('正在生成二维码…');
      try {
        const value = await local('/toy/call/phone/pair', {});
        box.replaceChildren(qrSvg(value.url));
        hint.textContent = t('二维码 10 分钟内有效，只能用一次。以后从手机桌面图标直接打（扫码后按页面提示添加到主屏幕）。');
      } catch (e) {
        hint.textContent = e.message === 'CALL_NETWORK' ? t('连不上服务器，请检查网络后重试。')
          : MESSAGES[e.message] || t('暂时没能生成二维码，请稍后再试。');
        box.hidden = true; retry.parentElement.hidden = false;
      }
    };
    retry.onclick = pair;
    back.onclick = () => {
      clearInterval(phoneTimer); panel.remove();
      shown.forEach(el => { el.hidden = el.dataset.wasHidden === '1'; delete el.dataset.wasHidden; });
    };
    pair(); refresh(); clearInterval(phoneTimer); phoneTimer = setInterval(refresh, 3000);
    back.focus();
  }

  class Call {
    constructor(ui) {
      this.ui = ui; this.transcript = []; this.turn = 0; this.stopped = new Set(); this.ended = false; this.her = '';
      this.herDone = new Set(); this.lastAudioAt = {}; this.drainedAt = {};
      this.firstAudioAt = {}; this.tones = []; this.avatar = null;
    }

    // The avatar follows the call: her voice and the microphone through analysers, events and tones.
    attach(avatar) {
      this.avatar = avatar;
      if (this.herMeter) avatar.meter('her', this.herMeter);
      if (this.micMeter) avatar.meter('user', this.micMeter);
      if (this.startedAt && !this.ended) { avatar.event('ready'); this.toneTimer = this.toneTimer || setInterval(() => this.toneDue(), 100); }
    }

    // Her words cue small reactions where she says them: ？ tilts, ！ hops, ～ sways, 哈哈 laughs, …… ponders.
    cues(turn, before, delta) {
      const text = before + delta;
      for (let i = before.length; i < text.length; i++) {
        const ch = text[i], two = text.slice(i - 1, i + 1);
        const cue = '？?'.includes(ch) ? 'question' : '！!'.includes(ch) ? 'exclaim' : '~～'.includes(ch) ? 'sway'
          : /^(哈哈|嘻嘻|嘿嘿|呵呵)$/.test(two) ? 'laugh' : ch === '…' && text[i - 1] !== '…' ? 'ponder' : null;
        if (cue && this.tones.length < 80) this.tones.push({turn, at: i, cue});
      }
    }

    // A tone change takes effect when her voice reaches it (her text runs ahead of her voice).
    toneDue() {
      const now = performance.now();
      this.tones = this.tones.filter(x => {
        if (this.stopped.has(x.turn)) return false;
        const start = this.firstAudioAt[x.turn];
        if (start === undefined) return true;
        if (now < start + 150 + x.at / 4.5 * 1000) return true;
        if (x.cue) this.avatar?.cue(x.cue); else this.avatar?.tone(x.tone);
        return false;
      });
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
      this.herMeter = this.context.createAnalyser(); this.herMeter.fftSize = 512; this.player.connect(this.herMeter);
      this.avatar?.meter('her', this.herMeter);
      this.ringing = this.ring();
      const minimum = new Promise(r => setTimeout(r, 2500));
      let started;
      try { started = await local('/toy/call/start', ui.model ? {model: ui.model} : {}); }
      catch (e) { await minimum; return this.fail(e.message); }
      if (started.pickup === 'missed') {
        await new Promise(r => setTimeout(r, 18000));
        return this.finish(t('暂时无人接听'), null);
      }
      if (started.cents_per_minute) ui.price.textContent = t('本次通话每分钟 {price}，按秒计费。', {price: yuan(started.cents_per_minute)});
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
      ws.onclose = () => {
        if (!this.ended) this.finish(this.startedAt ? t('通话已结束') : t('没能接通，请稍后再试'), null);
        if (!this.kept) this.recover();
      };
    }

    message(m) {
      const ui = this.ui;
      if (m.type === 'ready') {
        this.callId = m.call_id || null;
        this.ringing?.(); this.ringing = null; this.startedAt = Date.now(); ui.card.dataset.state = 'talking';
        ui.mute.hidden = false; this.clock = setInterval(() => this.tick(), 500); this.tick();
        this.capture(); this.worldTimer = setInterval(() => this.world(), 300000);
        if (this.avatar) { this.avatar.event('ready'); this.toneTimer = setInterval(() => this.toneDue(), 100); }
      } else if (m.type === 'user') {
        this.avatar?.event('user_done');
        this.recall(m.text);
      } else if (m.type === 'tone') {
        if (typeof m.tone === 'string' && typeof m.at === 'number' && this.tones.length < 50) this.tones.push(m);
      } else if (m.type === 'her') {
        if (m.turn !== this.turn) { this.turn = m.turn; this.her = ''; }
        if (!this.stopped.has(m.turn)) {
          if (this.avatar) this.cues(m.turn, this.her, m.delta);
          this.her += m.delta; ui.caption.textContent = this.her;
        }
      } else if (m.type === 'stop') {
        this.stopped.add(m.turn); this.player.port.postMessage({clear: true}); this.avatar?.event('stop');
        // The caption keeps only what she said aloud before she was cut off.
        if (typeof m.heard === 'string' && m.turn === this.turn) { this.her = m.heard; ui.caption.textContent = m.heard ? m.heard + '……' : ''; }
      } else if (m.type === 'hush') {
        this.player.port.postMessage({clear: true});   // her filler sound, when the user goes on talking
      } else if (m.type === 'her_done') {
        this.herDone.add(m.turn); this.avatar?.event('her_done');
        if ((this.drainedAt[m.turn] || 0) > (this.lastAudioAt[m.turn] || 0)) this.reportPlayed(m.turn);
      } else if (m.type === 'notice' && m.reason === 'limit') {
        ui.status.textContent = t('余额快用完了，通话即将结束');
      } else if (m.type === 'end') {
        if (this.ended) this.keep(m); else this.finish(t('通话结束'), m);
      }
    }

    audio(buffer) {
      const view = new DataView(buffer), turn = view.getUint32(0);
      if (this.stopped.has(turn)) return;
      this.lastAudioAt[turn] = performance.now();
      if (this.firstAudioAt[turn] === undefined) this.firstAudioAt[turn] = this.lastAudioAt[turn];
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

    // What the user just said may touch something she remembers: look it up locally and pass it on.
    async recall(text) {
      if (typeof text !== 'string' || text.trim().length < 4) return;
      this.recalled = this.recalled || new Set();
      try {
        const found = (await local('/toy/call/recall', {text})).memories || [];
        const fresh = found.filter(m => !this.recalled.has(m)).slice(0, 3);
        fresh.forEach(m => this.recalled.add(m));
        if (fresh.length && this.ws?.readyState === 1) this.ws.send(JSON.stringify({type: 'recall', memories: fresh}));
      } catch (_) {}
    }

    reportPlayed(turn) { if (this.ws?.readyState === 1) this.ws.send(JSON.stringify({type: 'played', turn})); }

    async capture() {
      const source = this.context.createMediaStreamSource(this.stream);
      this.mic = new AudioWorkletNode(this.context, 'olivia-mic');
      this.mic.port.onmessage = e => { if (this.ws?.readyState === 1) this.ws.send(e.data); };
      const sink = this.context.createGain(); sink.gain.value = 0;
      source.connect(this.mic); this.mic.connect(sink); sink.connect(this.context.destination);
      this.micMeter = this.context.createAnalyser(); this.micMeter.fftSize = 512; source.connect(this.micMeter);
      this.avatar?.meter('user', this.micMeter);
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
      this.ui.status.textContent = t('通话中 {time}', {time: String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0')});
    }

    hangup() {
      if (this.ws?.readyState === 1) {
        // Hang up at once; the server's report (charge, transcript, notes) follows on the open socket.
        this.ws.send(JSON.stringify({type: 'hangup'}));
        this.finish(t('通话结束'), null, true);
      } else this.finish(t('已取消'), null);
    }

    fail(code) { this.finish(MESSAGES[code] || t('暂时没能打通，请稍后再试'), null); }

    finish(text, report, awaitReport = false) {
      if (this.ended) return; this.ended = true;
      clearInterval(this.clock); clearInterval(this.worldTimer); clearInterval(this.toneTimer);
      if (this.avatar) { this.avatar.event('end'); this.avatar.meter('her', null); this.avatar.meter('user', null); }
      this.ringing?.(); this.stream?.getTracks().forEach(t => t.stop());
      this.player?.port.postMessage({clear: true});
      if (awaitReport) this.reportTimer = setTimeout(() => { try { this.ws?.close(); } catch (_) {} this.recover(); }, 30000);
      else { try { this.ws?.close(); } catch (_) {} }
      setTimeout(() => this.context?.close().catch(() => {}), 300);
      const ui = this.ui; ui.card.dataset.state = 'ended'; ui.mute.hidden = true;
      const seconds = report?.seconds ?? (this.startedAt ? Math.round((Date.now() - this.startedAt) / 1000) : 0);
      ui.status.textContent = text + (seconds ? t('  {minutes} 分 {seconds} 秒', {minutes: Math.floor(seconds / 60), seconds: seconds % 60}) : '');
      ui.hang.textContent = t('关闭'); ui.hang.dataset.kind = 'cancel'; ui.hang.onclick = close;
      if (report) this.keep(report);
      ui.done();
    }

    // The socket is gone without the end report: the cloud keeps it for a day; fetch it so the call
    // is still remembered (a few tries while the server finishes its notes).
    async recover(tries = 0) {
      if (this.kept || this.recovering || !this.callId) return;
      this.recovering = true;
      try { await local('/toy/call/recover', {call_id: this.callId}); this.kept = true; }
      catch (_) { if (tries < 5) setTimeout(() => this.recover(tries + 1), [5000, 15000, 30000, 60000, 120000][tries]); }
      finally { this.recovering = false; }
    }

    keep(report) {
      this.kept = true;
      clearTimeout(this.reportTimer);
      if (typeof report.charge_cents === 'number') this.ui.price.textContent = t('本次通话 {price}', {price: yuan(report.charge_cents)});
      if (report.transcript?.length && report.call_id) {
        local('/toy/call/end', {call_id: report.call_id, seconds: report.seconds, charge_cents: report.charge_cents,
          reason: report.reason, pickup: this.pickup, transcript: report.transcript, notes: report.notes}).catch(() => {});
      }
      try { this.ws?.close(); } catch (_) {}
    }
  }

  function boot(tries = 0) {
    // The page script runs from <head>: <body> does not exist yet (2.3.4+call.1-3 threw here and
    // took the whole settings page down with it).
    if (!document.body) { document.addEventListener('DOMContentLoaded', () => boot(tries), {once: true}); return; }
    const marker = document.querySelector('[data-olivia-companion-settings]');
    base = marker && marker.getAttribute('data-api-base');
    if (!base) { if (tries < 60) setTimeout(() => boot(tries + 1), 1000); return; }
    place();
    // The header is re-rendered on navigation; put the button back whenever it goes missing.
    new MutationObserver(() => { if (!placing) { placing = true; requestAnimationFrame(place); } })
      .observe(document.body, {childList: true, subtree: true});
    refreshConfig(); setInterval(refreshConfig, 300000);
  }
  boot();
})();
'''.replace('/*QR*/', QR_JAVASCRIPT)
# The avatar engine defines window.OliviaAvatar before the call page script uses it.
JAVASCRIPT = AVATAR_JAVASCRIPT + JAVASCRIPT
