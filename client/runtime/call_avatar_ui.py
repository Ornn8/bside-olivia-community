"""Her call avatar: a Live2D-style rig drawn with WebGL, the same engine as the phone page's avatar.js.

Injected before the call page script (runtime/call_ui.py). The rig and atlas come from the cloud through
/toy/call/avatar/files/<name>, to accounts that own the avatar. Generated from call-avatar/engine/avatar.js.
"""

JAVASCRIPT = r'''
/* Olivia call avatar: a Live2D-style rig drawn with WebGL from layered parts.
 *
 * rig.json + atlas come from tools/build_rig.py. Head parts sit on a flattened sphere, so turning the head
 * slides the features across the face (parallax by depth); hair parts are springs that bend from their
 * roots; eyes, mouth and arms swap variants; the iris is clipped by the eye white with the stencil buffer.
 *
 *   const a = await OliviaAvatar.create(canvas, baseUrl)   // null when WebGL or the files are unavailable
 *   a.event('ready' | 'user_done' | 'stop' | 'her_done' | 'end'); a.tone('开心'); a.act('wave')
 *   a.herLevel(rms); a.userLevel(rms); a.destroy()
 */
(function (root) {
  'use strict';
  const D2R = Math.PI / 180;
  const clamp = (x, a, b) => x < a ? a : x > b ? b : x;
  const smooth = t => t * t * (3 - 2 * t);

  // Smooth 1D noise for idle drift: sum of incommensurate sines, cheap and seamless.
  function drift(t, seed) {
    return (Math.sin(t * 0.31 + seed) * 0.5 + Math.sin(t * 0.73 + seed * 2.1) * 0.3 + Math.sin(t * 1.37 + seed * 3.7) * 0.2);
  }

  class Spring {
    constructor(v, k) { this.v = v; this.t = v; this.vel = 0; this.k = k; }
    step(dt) { const a = this.k * this.k * (this.t - this.v) - 2 * this.k * this.vel; this.vel += a * dt; this.v += this.vel * dt; }
  }

  const vertexShader = 'attribute vec2 p;attribute vec2 uv;uniform vec4 fit;varying vec2 v;' +
    'void main(){v=uv;vec2 c=p*fit.xy+fit.zw;gl_Position=vec4(c.x,-c.y,0.,1.);}';
  // cut > 0 only for the eye white: transparent pixels must not stamp the stencil the iris is clipped to.
  const fragmentShader = 'precision mediump float;uniform sampler2D tex;uniform float alpha;uniform float cut;varying vec2 v;' +
    'void main(){vec4 c=texture2D(tex,v);if(c.a<cut)discard;gl_FragColor=c*alpha;}';

  function texture(gl, image) {
    const t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, !(window.ImageBitmap && image instanceof ImageBitmap));
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  }

  // Images arrive as blobs (the files need the caller's token); createImageBitmap avoids img-src blob: URLs.
  async function decode(blob) {
    if (window.createImageBitmap) {
      try { return await createImageBitmap(blob, {premultiplyAlpha: 'premultiply'}); } catch (_) {}
    }
    const url = URL.createObjectURL(blob);
    try {
      return await new Promise((done, fail) => {
        const img = new Image(); img.onload = () => done(img); img.onerror = () => fail(new Error('AVATAR_IMAGE')); img.src = url;
      });
    } finally { setTimeout(() => URL.revokeObjectURL(url), 1000); }
  }

  // Expression presets the choreography and the tone tags choose from.
  const TONES = {
    '开心': {eyes: 'smile', mouth: 'smile', blush: 0.25, brow: 0.3, gesture: ['cheer', 'tilt', 'nod'], fx: 'sparkle'},
    '温柔': {eyes: 'open', mouth: 'smile', blush: 0.2, brow: 0.1, gesture: ['tilt', 'nod']},
    '撒娇': {eyes: 'open', mouth: 'pout', blush: 0.6, brow: 0.2, gesture: ['tilt', 'shy'], fx: 'hearts'},
    '委屈': {eyes: 'open', mouth: 'frown', blush: 0.1, brow: -0.6, browAngle: 1, gaze: [0, 0.4], gesture: ['sad'], fx: 'tear'},
    '惊讶': {eyes: 'open', mouth: 'o', blush: 0, brow: 1, gesture: ['surprise'], fx: 'exclaim'},
    '害羞': {eyes: 'open', mouth: 'smile', blush: 1, brow: 0.1, gaze: [0.3, 0.5], gesture: ['shy'], fx: 'shy_lines'},
    '困倦': {eyes: 'half', mouth: 'neutral', blush: 0, brow: -0.2, gesture: [], fx: 'zzz'},
    '小声': {eyes: 'open', mouth: 'neutral', blush: 0, brow: 0, gaze: [0.4, 0], gesture: []},
    '平常': {eyes: 'open', mouth: 'smile', blush: 0, brow: 0, gesture: ['nod']},
    '笑': {eyes: 'smile', mouth: 'smile', blush: 0.3, brow: 0.2, gesture: ['giggle']},
  };

  // Actions: time-boxed overlays. apply(t, env, o) writes into the frame's targets; env eases in and out.
  const ACTIONS = {
    wave: {dur: 1.9, apply: (t, e, o) => { o.arm_R = 'wave'; o.armRotR += e * 9 * Math.sin(t * 15); o.eyes = 'smile'; o.angleZ += e * 4; }},
    nod: {dur: 0.9, apply: (t, e, o) => { o.angleY -= 7 * Math.sin(t * 7) * (1 - t / 0.9); }},
    shake: {dur: 1.1, apply: (t, e, o) => { o.angleX += 9 * Math.sin(t * 14) * (1 - t / 1.1); }},
    tilt: {dur: 1.8, apply: (t, e, o) => { o.angleZ += e * 9; o.angleX += e * 4; }},
    think: {dur: 3, apply: (t, e, o) => { o.arm_R = 'chin'; o.gazeX += e * 0.6; o.gazeY -= e * 0.7; o.angleZ -= e * 6; o.angleY += e * 4; o.brow += e * 0.4; o.mouth = 'neutral'; }},
    giggle: {dur: 1.6, apply: (t, e, o) => { o.arm_L = 'mouth'; o.eyes = 'smile'; o.bounce += e * 4 * Math.abs(Math.sin(t * 11)); o.blush += e * 0.3; }},
    shy: {dur: 2.6, apply: (t, e, o) => { o.arms = 'shy'; o.blush += e; o.gazeY += e * 0.6; o.gazeX += e * 0.3; o.angleY -= e * 6; o.angleZ += e * 5; }},
    cheer: {dur: 1.7, apply: (t, e, o) => { o.arms = 'cheer'; o.eyes = 'smile'; o.mouth = 'a_mid'; o.bounce += e * 7 * Math.abs(Math.sin(t * 8)); }},
    point: {dur: 1.7, apply: (t, e, o) => { o.arm_R = 'point'; o.brow += e * 0.7; o.angleZ -= e * 4; }},
    pout: {dur: 2.2, apply: (t, e, o) => { o.mouth = 'pout'; o.browAngle -= e; o.angleZ -= e * 6; o.angleX -= e * 6; }},
    sad: {dur: 2.6, apply: (t, e, o) => { o.mouth = 'frown'; o.browAngle += e; o.brow -= e * 0.5; o.gazeY += e * 0.5; o.angleY -= e * 5; }},
    surprise: {dur: 1.3, apply: (t, e, o) => { o.mouth = 'o'; o.brow += e; o.bounce += e * 5 * Math.max(0, Math.sin(t * 6)); }},
    look: {dur: 2.4, apply: (t, e, o) => { o.gazeX += e * 0.8; o.angleX += e * 8; }},
    hello: {dur: 2.2, apply: (t, e, o) => { ACTIONS.wave.apply(t, e, o); o.mouth = 'a_small'; }},
    bye: {dur: 2.4, apply: (t, e, o) => { ACTIONS.wave.apply(t, e, o); o.angleZ += e * 6; }},
  };
  const FX_PLACE = {  // where an effect appears, in head radii from the head centre, and how it moves
    heart: [0.9, -0.8, 'float'], hearts: [0.85, -0.9, 'float'], sparkle: [-0.9, -0.7, 'pop'], sweat: [-0.95, -0.35, 'drip'],
    anger: [0.75, -0.75, 'pulse'], question: [0.95, -0.95, 'pop'], exclaim: [0.95, -0.95, 'pop'], music: [0.95, -0.7, 'float'],
    zzz: [0.9, -0.9, 'float'], tear: [0.42, 0.25, 'drip'], shy_lines: [0, 0.32, 'pulse'], bulb: [-0.95, -1.0, 'pop'],
  };

  class Avatar {
    // source: a base URL, or a function name -> fetch Response (for files that need a token).
    static async create(canvas, source) {
      const gl = canvas.getContext('webgl', {alpha: true, premultipliedAlpha: true, stencil: true, antialias: true});
      if (!gl) return null;
      const get = typeof source === 'function' ? source : name => fetch(source.replace(/\/?$/, '/') + name);
      const file = async name => { const r = await get(name); if (!r.ok) throw new Error('AVATAR_FILE'); return r; };
      const rig = await (await file('rig.json')).json();
      const atlas = await decode(await (await file(rig.atlas_file || 'atlas.webp')).blob());
      const fx = {};
      await Promise.all((rig.fx || []).map(async f => {
        try { fx[f.name] = {img: await decode(await (await file(f.file)).blob()), w: f.w, h: f.h}; } catch (_) {}
      }));
      return new Avatar(canvas, gl, rig, atlas, fx);
    }

    constructor(canvas, gl, rig, atlas, fx) {
      this.canvas = canvas; this.gl = gl; this.rig = rig;
      this.program = this.compile();
      this.atlas = texture(gl, atlas);
      this.fxTex = {};
      for (const name in fx) this.fxTex[name] = {tex: texture(gl, fx[name].img), w: fx[name].w, h: fx[name].h};
      this.buffer = gl.createBuffer(); this.indexBuffer = gl.createBuffer();
      this.parts = rig.parts.map(p => this.mesh(p));
      this.byName = {}; for (const p of this.parts) this.byName[p.name] = p;
      this.slots = {};  // slot -> {variant: alpha}
      const s = (v, k) => new Spring(v, k);
      this.p = {angleX: s(0, 6), angleY: s(0, 6), angleZ: s(0, 5), bodyX: s(0, 4), gazeX: s(0, 9), gazeY: s(0, 9),
        brow: s(0, 10), browAngle: s(0, 10), blush: s(0, 4), armRotL: s(0, 14), armRotR: s(0, 14), bounce: s(0, 18),
        mouthOpen: s(0, 28)};
      this.phys = {};
      for (const p of this.parts) if (p.physics) this.phys[p.name] = {x: 0, v: 0};
      this.state = 'idle'; this.stateAt = 0; this.mood = '平常'; this.moodAt = -99;
      this.actions = []; this.effects = [];
      this.t = 0; this.blinkAt = 1.5; this.blink = 0; this.nextIdle = 6; this.nextNod = 0;
      this.her = 0; this.herPeak = 0; this.herAvg = 0; this.herOn = false; this.herQuietAt = 0; this.form = 'a';
      this.user = 0; this.userOn = false; this.userQuietAt = 0; this.lastSpeech = 0;
      this.prevHead = null; this.headVel = 0;
      this.frame = this.frame.bind(this); this.last = performance.now(); this.raf = requestAnimationFrame(this.frame);
    }

    compile() {
      const gl = this.gl, sh = (type, src) => { const x = gl.createShader(type); gl.shaderSource(x, src); gl.compileShader(x); return x; };
      const pr = gl.createProgram();
      gl.attachShader(pr, sh(gl.VERTEX_SHADER, vertexShader)); gl.attachShader(pr, sh(gl.FRAGMENT_SHADER, fragmentShader)); gl.linkProgram(pr);
      if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) throw new Error('AVATAR_SHADER');
      this.loc = {p: gl.getAttribLocation(pr, 'p'), uv: gl.getAttribLocation(pr, 'uv'), fit: gl.getUniformLocation(pr, 'fit'),
        alpha: gl.getUniformLocation(pr, 'alpha'), tex: gl.getUniformLocation(pr, 'tex'), cut: gl.getUniformLocation(pr, 'cut')};
      return pr;
    }

    mesh(p) {
      const [cols, rows] = p.grid || [2, 2], n = (cols + 1) * (rows + 1), [aw, ah] = this.rig.atlas;
      const base = new Float32Array(n * 2), data = new Float32Array(n * 4), index = [];
      for (let j = 0; j <= rows; j++) for (let i = 0; i <= cols; i++) {
        const k = j * (cols + 1) + i;
        base[k * 2] = p.x + p.w * i / cols; base[k * 2 + 1] = p.y + p.h * j / rows;
        data[k * 4 + 2] = (p.u + p.w * i / cols) / aw; data[k * 4 + 3] = (p.v + p.h * j / rows) / ah;
        if (i < cols && j < rows) index.push(k, k + 1, k + cols + 1, k + 1, k + cols + 2, k + cols + 1);
      }
      return Object.assign({}, p, {base, data, index: new Uint16Array(index), alpha: 1});
    }

    // ----- inputs from the call -----
    event(name) {
      if (name === 'ready') { this.state = 'idle'; this.act('hello'); }
      else if (name === 'end') { this.state = 'idle'; this.her = this.user = 0; this.act('bye'); }
      else if (name === 'user_done') { if (this.state !== 'speaking') this.setState('thinking'); }
      else if (name === 'stop' || name === 'her_done') { if (this.state === 'speaking') this.setState('listening'); }
    }
    tone(name) {
      if (!TONES[name]) return;
      this.mood = name; this.moodAt = this.t;
      const preset = TONES[name];
      if (preset.fx && Math.random() < 0.7) this.spawn(preset.fx);
      const g = preset.gesture;
      if (g.length && this.t - this.lastGesture > 4 && Math.random() < 0.6) { this.act(g[Math.floor(Math.random() * g.length)]); }
    }
    herLevel(rms) { this.her = rms; }
    userLevel(rms) { this.user = rms; }
    // An AnalyserNode on her voice or the microphone: read once per frame, in step with what is heard.
    meter(who, analyser) { this.meters = this.meters || {}; this.meters[who] = analyser; }
    sample() {
      if (!this.meters) return;
      for (const who in this.meters) {
        const node = this.meters[who]; if (!node) continue;
        const buf = this.buf && this.buf.length === node.fftSize ? this.buf : (this.buf = new Float32Array(node.fftSize));
        node.getFloatTimeDomainData(buf);
        let sum = 0; for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
        this[who] = Math.sqrt(sum / buf.length);
      }
    }
    act(name) {
      const a = ACTIONS[name]; if (!a) return;
      this.actions = this.actions.filter(x => x.name !== name);
      this.actions.push({name, at: this.t, dur: a.dur, apply: a.apply});
      if (!['nod', 'look', 'tilt'].includes(name)) this.lastGesture = this.t;
    }
    spawn(name) { if (this.fxTex[name] && FX_PLACE[name]) this.effects.push({name, at: this.t, dur: 1.8}); }
    setState(s) { if (this.state !== s) { this.state = s; this.stateAt = this.t; } }

    // ----- choreography: base pose for the current state, then overlays -----
    targets(dt) {
      const t = this.t, mood = TONES[this.mood] || TONES['平常'];
      const o = {angleX: drift(t * 0.6, 1) * 6, angleY: drift(t * 0.5, 7) * 4, angleZ: drift(t * 0.4, 3) * 3,
        bodyX: drift(t * 0.3, 5), gazeX: drift(t * 0.7, 11) * 0.25, gazeY: drift(t * 0.6, 13) * 0.15,
        brow: mood.brow || 0, browAngle: mood.browAngle || 0, blush: mood.blush || 0, armRotL: 0, armRotR: 0, bounce: 0,
        eyes: mood.eyes, mouth: mood.mouth, arm_L: 'down', arm_R: 'down', arms: null};
      if (mood.gaze) { o.gazeX += mood.gaze[0]; o.gazeY += mood.gaze[1]; }
      if (this.t - this.moodAt > 12 && this.mood !== '平常') this.mood = '平常';

      // Voice activity with hangover, so syllable gaps do not flicker the state.
      if (this.her > 0.012) { this.herOn = true; this.herQuietAt = t; this.lastSpeech = t; }
      else if (this.herOn && t - this.herQuietAt > 0.5) this.herOn = false;
      if (this.user > 0.03) { this.userOn = true; this.userQuietAt = t; }
      else if (this.userOn && t - this.userQuietAt > 0.7) { this.userOn = false; if (this.state === 'listening') this.setState('thinking'); }
      if (this.herOn) this.setState('speaking');
      else if (this.userOn && this.state !== 'speaking') this.setState('listening');
      else if (this.state === 'speaking' && !this.herOn) this.setState('idle');
      if (this.state === 'thinking' && t - this.stateAt > 7) this.setState('idle');

      if (this.state === 'listening') {
        o.gazeX *= 0.3; o.gazeY *= 0.3; o.brow += 0.2; o.angleZ += 3;
        if (this.user > 0.08 && t > this.nextNod) { this.act('nod'); this.nextNod = t + 1.8 + Math.random() * 2; }
      } else if (this.state === 'thinking') {
        const k = smooth(clamp((t - this.stateAt - 0.5) / 0.6, 0, 1));  // only a real wait shows thinking
        if (k > 0 && !this.thinkActed) { this.thinkActed = true; if (Math.random() < 0.45) this.act('think'); }
        o.gazeX += k * 0.55; o.gazeY -= k * 0.6; o.angleZ -= k * 4; o.mouth = 'neutral';
      } else if (this.state === 'speaking') {
        this.herAvg += (this.her - this.herAvg) * Math.min(1, dt * 3);
        if (this.her > this.herAvg * 1.9 && this.her > 0.05 && t > this.nextNod) { this.act('nod'); this.nextNod = t + 1.2 + Math.random() * 2.5; }
        if (t - this.lastGesture > 7 + Math.random() * 6 && (mood.gesture || []).length && Math.random() < dt * 0.4) {
          this.act(mood.gesture[Math.floor(Math.random() * mood.gesture.length)]);
        }
      } else if (this.state === 'idle' && t > this.nextIdle) {
        this.act(['look', 'tilt', 'nod'][Math.floor(Math.random() * 3)]); this.nextIdle = t + 7 + Math.random() * 8;
      }
      if (this.state !== 'thinking') this.thinkActed = false;

      this.actions = this.actions.filter(a => t - a.at < a.dur);
      for (const a of this.actions) {
        const at = t - a.at, e = smooth(clamp(Math.min(at / 0.25, (a.dur - at) / 0.35), 0, 1));
        a.apply(at, e, o);
      }
      if (this.override) Object.assign(o, this.override);  // test page sliders
      return o;
    }

    frame(now) {
      this.raf = requestAnimationFrame(this.frame);
      const dt = clamp((now - this.last) / 1000, 0, 0.05); this.last = now;
      if (document.hidden) return;
      this.t += dt;
      this.sample();
      const o = this.targets(dt), p = this.p;
      for (const k of ['angleX', 'angleY', 'angleZ', 'bodyX', 'gazeX', 'gazeY', 'brow', 'browAngle', 'blush', 'armRotL', 'armRotR', 'bounce']) {
        p[k].t = o[k];
      }
      // Lip sync: openness follows her voice, a new syllable picks a new mouth shape.
      const open = this.state === 'speaking' ? clamp((this.her - 0.008) * 7, 0, 1) : 0;
      if (open > 0.35 && p.mouthOpen.v < 0.2) this.form = Math.random() < 0.6 ? 'a' : Math.random() < 0.5 ? 'o' : 'e';
      p.mouthOpen.t = open;
      for (const k in p) p[k].step(dt);

      // Blink: every 2-6 s, sometimes twice.
      if (this.t > this.blinkAt) { this.blink = 0.16; this.blinkAt = this.t + (Math.random() < 0.15 ? 0.3 : 2 + Math.random() * 4); }
      this.blink = Math.max(0, this.blink - dt);
      const lid = this.blink > 0 ? (this.blink > 0.11 || this.blink < 0.04 ? 'half' : 'closed') : null;

      const m = p.mouthOpen.v;
      let mouth = o.mouth;
      if (m > 0.12) mouth = this.form === 'o' && m > 0.3 ? 'o' : this.form === 'e' && m > 0.3 ? 'e' : m < 0.4 ? 'a_small' : m < 0.7 ? 'a_mid' : 'a_wide';
      const eyes = lid || o.eyes || 'open';
      this.show('eye_L', this.has('eye_L', eyes) ? eyes : 'open', 60); this.show('eye_R', this.has('eye_R', eyes) ? eyes : 'open', 60);
      this.show('mouth', this.has('mouth', mouth) ? mouth : 'smile', 60);
      this.show('arms', o.arms, 9);
      this.show('arm_L', o.arms ? null : o.arm_L, 9); this.show('arm_R', o.arms ? null : o.arm_R, 9);
      this.slotAlpha('blush', 'on', clamp(p.blush.v, 0, 1));
      this.fade(dt);
      this.physics(dt);
      this.draw();
    }

    has(slot, variant) { return this.parts.some(q => q.slot === slot && q.variant === variant); }
    show(slot, variant, speed) { this.slots[slot] = this.slots[slot] || {target: null, speed: 10, alpha: {}}; this.slots[slot].target = variant; this.slots[slot].speed = speed; }
    slotAlpha(slot, variant, a) { this.slots[slot] = {target: variant, speed: 0, alpha: {[variant]: a}, fixed: true}; }
    fade(dt) {
      for (const name in this.slots) {
        const s = this.slots[name]; if (s.fixed) continue;
        const variants = new Set(this.parts.filter(q => q.slot === name).map(q => q.variant));
        for (const v of variants) {
          const target = v === s.target ? 1 : 0, cur = s.alpha[v] ?? target;
          s.alpha[v] = s.speed >= 60 ? target : cur + clamp(target - cur, -s.speed * dt, s.speed * dt);
        }
      }
    }
    alphaOf(part) {
      if (!part.slot) return 1;
      const s = this.slots[part.slot]; return s ? (s.alpha[part.variant] ?? 0) : (part.variant === 'down' || part.variant === 'open' || part.variant === 'smile' ? 1 : 0);
    }

    // Hair springs: driven by the head's sideways acceleration and pulled down by gravity when the head rolls.
    physics(dt) {
      const p = this.p, h = this.rig.head;
      const headX = Math.sin(p.angleX.v * D2R) * h.radius * 0.5 + Math.sin(p.angleZ.v * D2R) * h.radius + p.bodyX.v * 6;
      if (this.prevHead === null) this.prevHead = headX;
      const vel = (headX - this.prevHead) / Math.max(dt, 1e-3); this.prevHead = headX;
      const acc = (vel - this.headVel) / Math.max(dt, 1e-3); this.headVel = vel;
      for (const part of this.parts) {
        if (!part.physics) continue;
        const c = this.rig.physics[part.physics], s = this.phys[part.name];
        const hang = -Math.sin(p.angleZ.v * D2R) * part.h * 0.25;
        const a = -c.k * (s.x - hang) - c.c * s.v - clamp(acc, -4000, 4000) * c.inertia * 0.02;
        s.v += a * dt; s.x = clamp(s.x + s.v * dt, -part.h * 0.25, part.h * 0.25);
      }
    }

    // Head-local point -> sphere parallax (yaw, pitch) -> roll about the neck -> body motion.
    head(x, y, depth) {
      const h = this.rig.head, R = h.radius * 1.15, p = this.p;
      const yaw = p.angleX.v * D2R, pitch = p.angleY.v * D2R, roll = p.angleZ.v * D2R;
      const u = (x - h.center[0]) / R, v = (y - h.center[1]) / R;
      const z = Math.sqrt(Math.max(0, 1 - u * u - v * v)) * 0.6 + depth;
      const u2 = u * Math.cos(yaw) + z * Math.sin(yaw), z2 = -u * Math.sin(yaw) + z * Math.cos(yaw);
      const v2 = v * Math.cos(pitch) - z2 * Math.sin(pitch);
      let X = h.center[0] + u2 * R, Y = h.center[1] + v2 * R;
      const dx = X - h.neck[0], dy = Y - h.neck[1], c = Math.cos(roll), s = Math.sin(roll);
      X = h.neck[0] + dx * c - dy * s; Y = h.neck[1] + dx * s + dy * c;
      return this.body(X, Y, h.neck[1]);
    }
    body(x, y, refY) {
      const p = this.p, breath = Math.sin(this.t * 1.9) * 0.5 + 0.5, top = this.rig.head.neck[1], bottom = this.rig.canvas[1];
      const w = clamp((bottom - (refY ?? y)) / (bottom - top), 0, 1);
      return [x + p.bodyX.v * 6 * w, y - breath * 4 * w - p.bounce.v];
    }

    vertex(part, k) {
      let x = part.base[k * 2], y = part.base[k * 2 + 1];
      const p = this.p, h = this.rig.head;
      if (part.physics) {
        const w = clamp((y - part.root_y) / Math.max(1, part.y + part.h - part.root_y), 0, 1);
        const bend = Math.pow(w, this.rig.physics[part.physics].bend), sx = this.phys[part.name].x;
        x += sx * bend; y -= Math.abs(sx) * 0.12 * bend;
      }
      if (part.gaze) { x += p.gazeX.v * part.w * 0.16; y += p.gazeY.v * part.h * 0.12; }
      if (part.brow) {
        const side = part.brow === 'L' ? 1 : -1, cx = part.x + part.w / 2;
        y -= p.brow.v * h.radius * 0.05;
        y += p.browAngle.v * side * (x - cx) * -0.12;  // +1 raises the inner ends (sad), -1 lowers them (cross)
      }
      if (part.group === 'head') return this.head(x, y, part.depth || 0);
      if (part.group === 'arm_L' || part.group === 'arm_R') {
        const side = part.group.slice(-1), pivot = this.rig.anchors['shoulder_' + side];
        const rot = (side === 'L' ? p.armRotL.v : p.armRotR.v) * D2R;
        if (pivot && rot) {
          const dx = x - pivot[0], dy = y - pivot[1], c = Math.cos(rot), s = Math.sin(rot);
          x = pivot[0] + dx * c - dy * s; y = pivot[1] + dx * s + dy * c;
        }
      }
      return this.body(x, y);
    }

    draw() {
      const gl = this.gl, c = this.canvas, dpr = Math.min(2, window.devicePixelRatio || 1);
      const width = Math.round(c.clientWidth * dpr), height = Math.round(c.clientHeight * dpr);
      if (c.width !== width || c.height !== height) { c.width = width; c.height = height; }
      gl.viewport(0, 0, width, height);
      gl.clearColor(0, 0, 0, 0); gl.clearStencil(0); gl.clear(gl.COLOR_BUFFER_BIT | gl.STENCIL_BUFFER_BIT);
      gl.useProgram(this.program); gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      // Fit the rig canvas into the element, bottom-centred.
      const [rw, rh] = this.rig.canvas, scale = Math.min(width / rw, height / rh);
      const fit = [2 * scale / width, 2 * scale / height, -1 + (width - rw * scale) / width, -1 + 2 * (height - rh * scale) / height];
      gl.uniform4f(this.loc.fit, fit[0], fit[1], fit[2], fit[3]);
      gl.activeTexture(gl.TEXTURE0); gl.uniform1i(this.loc.tex, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.indexBuffer);
      gl.enableVertexAttribArray(this.loc.p); gl.enableVertexAttribArray(this.loc.uv);
      gl.bindTexture(gl.TEXTURE_2D, this.atlas);
      let stencilRef = 0;
      for (const part of this.parts) {
        const alpha = this.alphaOf(part);
        if (alpha <= 0.003) continue;
        const n = part.base.length / 2;
        for (let k = 0; k < n; k++) { const [x, y] = this.vertex(part, k); part.data[k * 4] = x; part.data[k * 4 + 1] = y; }
        if (part.mask) {  // eye white: stamp the stencil the iris is clipped to
          stencilRef = part.name.indexOf('_L_') > 0 ? 1 : 2;
          gl.enable(gl.STENCIL_TEST); gl.stencilFunc(gl.ALWAYS, stencilRef, 0xff); gl.stencilOp(gl.KEEP, gl.KEEP, gl.REPLACE);
        } else if (part.clip) {
          gl.enable(gl.STENCIL_TEST); gl.stencilFunc(gl.EQUAL, part.clip.indexOf('_L_') > 0 ? 1 : 2, 0xff); gl.stencilOp(gl.KEEP, gl.KEEP, gl.KEEP);
        } else gl.disable(gl.STENCIL_TEST);
        gl.uniform1f(this.loc.cut, part.mask ? 0.5 : 0);
        this.drawData(part.data, part.index, alpha);
      }
      gl.uniform1f(this.loc.cut, 0);
      gl.disable(gl.STENCIL_TEST);
      this.drawEffects();
    }

    drawData(data, index, alpha) {
      const gl = this.gl;
      gl.bufferData(gl.ARRAY_BUFFER, data, gl.STREAM_DRAW);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, index, gl.STREAM_DRAW);
      gl.vertexAttribPointer(this.loc.p, 2, gl.FLOAT, false, 16, 0);
      gl.vertexAttribPointer(this.loc.uv, 2, gl.FLOAT, false, 16, 8);
      gl.uniform1f(this.loc.alpha, alpha);
      gl.drawElements(gl.TRIANGLES, index.length, gl.UNSIGNED_SHORT, 0);
    }

    drawEffects() {
      const h = this.rig.head, quad = new Uint16Array([0, 1, 2, 1, 3, 2]);
      this.effects = this.effects.filter(e => this.t - e.at < e.dur);
      for (const e of this.effects) {
        const tex = this.fxTex[e.name], place = FX_PLACE[e.name], k = (this.t - e.at) / e.dur;
        let [ox, oy] = [place[0] * h.radius, place[1] * h.radius], s = h.radius * 0.0028, a = 1;
        if (place[2] === 'float') { oy -= k * h.radius * 0.35; a = 1 - smooth(clamp((k - 0.6) / 0.4, 0, 1)); }
        else if (place[2] === 'pop') { s *= k < 0.15 ? smooth(k / 0.15) * 1.15 : 1; a = 1 - smooth(clamp((k - 0.7) / 0.3, 0, 1)); }
        else if (place[2] === 'drip') { oy += k * h.radius * 0.25; a = 1 - smooth(clamp((k - 0.5) / 0.5, 0, 1)); }
        else if (place[2] === 'pulse') { s *= 1 + Math.sin(k * 18) * 0.08; a = 1 - smooth(clamp((k - 0.7) / 0.3, 0, 1)); }
        const [cx, cy] = this.head(h.center[0] + ox, h.center[1] + oy, 0.3), w = tex.w * s * 2, hh = tex.h * s * 2;
        const data = new Float32Array([cx - w / 2, cy - hh / 2, 0, 0, cx + w / 2, cy - hh / 2, 1, 0, cx - w / 2, cy + hh / 2, 0, 1, cx + w / 2, cy + hh / 2, 1, 1]);
        this.gl.bindTexture(this.gl.TEXTURE_2D, tex.tex);
        this.drawData(data, quad, a);
      }
      this.gl.bindTexture(this.gl.TEXTURE_2D, this.atlas);
    }

    destroy() {
      cancelAnimationFrame(this.raf);
      const gl = this.gl; gl.deleteTexture(this.atlas); for (const k in this.fxTex) gl.deleteTexture(this.fxTex[k].tex);
      gl.deleteBuffer(this.buffer); gl.deleteBuffer(this.indexBuffer); gl.deleteProgram(this.program);
    }
  }
  Avatar.prototype.lastGesture = -99;
  Avatar.ACTIONS = Object.keys(ACTIONS); Avatar.TONES = Object.keys(TONES);
  root.OliviaAvatar = Avatar;
})(window);
'''
