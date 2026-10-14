"""A small QR encoder for the call page's phone-pairing code (no image library in the client).

Checked against OpenCV's decoders for versions 1-10 and all eight masks (tests/call/test_phone_line.py).
"""

JAVASCRIPT = r'''
  // QR code (byte mode, error correction M, versions 1-10): enough for a pairing link of ~60 characters.
  // After Project Nayuki's reference QR generator (MIT); returns rows of booleans (true = dark).
  function qrMatrix(text) {
    const ECC = [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26], BLOCKS = [0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5];
    const bytes = Array.from(new TextEncoder().encode(text));
    const raw = v => { let r = (16 * v + 128) * v + 64; if (v >= 2) { const n = Math.floor(v / 7) + 2; r -= (25 * n - 10) * n - 55; if (v >= 7) r -= 36; } return r; };
    const dataCap = v => Math.floor(raw(v) / 8) - ECC[v] * BLOCKS[v];
    let ver = 1;
    while (ver <= 10 && 4 + (ver < 10 ? 8 : 16) + bytes.length * 8 > dataCap(ver) * 8) ver++;
    if (ver > 10) throw Error('QR_TOO_LONG');
    const bits = [], put = (value, n) => { for (let i = n - 1; i >= 0; i--) bits.push((value >>> i) & 1); };
    put(4, 4); put(bytes.length, ver < 10 ? 8 : 16); bytes.forEach(b => put(b, 8));
    const capBits = dataCap(ver) * 8;
    put(0, Math.min(4, capBits - bits.length)); put(0, (8 - bits.length % 8) % 8);
    for (let pad = 0xEC; bits.length < capBits; pad ^= 0xEC ^ 0x11) put(pad, 8);
    const data = []; for (let i = 0; i < bits.length; i += 8) data.push(bits.slice(i, i + 8).reduce((a, b) => a * 2 + b, 0));
    // Reed-Solomon over GF(256), polynomial 0x11D.
    const mul = (x, y) => { let z = 0; for (let i = 7; i >= 0; i--) { z = (z << 1) ^ ((z >>> 7) * 0x11D); z ^= ((y >>> i) & 1) * x; } return z & 255; };
    const degree = ECC[ver], gen = new Array(degree).fill(0); gen[degree - 1] = 1;
    for (let i = 0, root = 1; i < degree; i++, root = mul(root, 2)) {
      for (let j = 0; j < degree; j++) { gen[j] = mul(gen[j], root); if (j + 1 < degree) gen[j] ^= gen[j + 1]; }
    }
    const rs = block => { const out = new Array(degree).fill(0);
      for (const b of block) { const f = b ^ out.shift(); out.push(0); gen.forEach((g, i) => { out[i] ^= mul(g, f); }); } return out; };
    const nBlocks = BLOCKS[ver], total = Math.floor(raw(ver) / 8), short = nBlocks - total % nBlocks, shortLen = Math.floor(total / nBlocks);
    const blocks = [];
    for (let i = 0, k = 0; i < nBlocks; i++) {
      const len = shortLen - degree + (i < short ? 0 : 1), part = data.slice(k, k + len); k += len;
      const ecc = rs(part); if (i < short) part.push(null); blocks.push(part.concat(ecc));
    }
    const codewords = [];
    for (let i = 0; i < blocks[0].length; i++) blocks.forEach(b => { if (b[i] !== null) codewords.push(b[i]); });
    // Function patterns.
    const size = ver * 4 + 17, grid = [], fixed = [];
    for (let y = 0; y < size; y++) { grid.push(new Array(size).fill(false)); fixed.push(new Array(size).fill(false)); }
    const set = (x, y, dark) => { grid[y][x] = dark; fixed[y][x] = true; };
    for (let i = 0; i < size; i++) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }
    const finder = (cx, cy) => { for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) {
      const d = Math.max(Math.abs(dx), Math.abs(dy)), x = cx + dx, y = cy + dy;
      if (x >= 0 && x < size && y >= 0 && y < size) set(x, y, d !== 2 && d !== 4); } };
    finder(3, 3); finder(size - 4, 3); finder(3, size - 4);
    const align = [];
    if (ver > 1) { const n = Math.floor(ver / 7) + 2, step = Math.floor((ver * 8 + n * 3 + 5) / (n * 4 - 4)) * 2;
      align.push(6); for (let i = 0, p = size - 7; i < n - 1; i++, p -= step) align.splice(1, 0, p); }
    align.forEach((ay, i) => align.forEach((ax, j) => {
      if ((i === 0 && j === 0) || (i === 0 && j === align.length - 1) || (i === align.length - 1 && j === 0)) return;
      for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) set(ax + dx, ay + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
    }));
    const format = mask => {
      const value = (0 << 3) | mask; let rem = value;   // M = 0b00
      for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
      const f = ((value << 10) | rem) ^ 0x5412, bit = i => ((f >>> i) & 1) === 1;
      for (let i = 0; i <= 5; i++) set(8, i, bit(i));
      set(8, 7, bit(6)); set(8, 8, bit(7)); set(7, 8, bit(8));
      for (let i = 9; i < 15; i++) set(14 - i, 8, bit(i));
      for (let i = 0; i < 8; i++) set(size - 1 - i, 8, bit(i));
      for (let i = 8; i < 15; i++) set(8, size - 15 + i, bit(i));
      set(8, size - 8, true);
    };
    format(0);
    if (ver >= 7) {
      let rem = ver; for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1F25);
      const v = (ver << 12) | rem;
      for (let i = 0; i < 18; i++) { const d = ((v >>> i) & 1) === 1, a = size - 11 + i % 3, b = Math.floor(i / 3); set(a, b, d); set(b, a, d); }
    }
    // Codewords in the zigzag order.
    let bit = 0;
    for (let right = size - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (let vert = 0; vert < size; vert++) for (let j = 0; j < 2; j++) {
        const x = right - j, upward = ((right + 1) & 2) === 0, y = upward ? size - 1 - vert : vert;
        if (!fixed[y][x] && bit < codewords.length * 8) { grid[y][x] = ((codewords[bit >>> 3] >>> (7 - (bit & 7))) & 1) === 1; bit++; }
      }
    }
    const MASKS = [(x, y) => (x + y) % 2, (x, y) => y % 2, (x, y) => x % 3, (x, y) => (x + y) % 3,
      (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2, (x, y) => x * y % 2 + x * y % 3,
      (x, y) => (x * y % 2 + x * y % 3) % 2, (x, y) => ((x + y) % 2 + x * y % 3) % 2];
    const apply = m => { for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) if (!fixed[y][x] && MASKS[m](x, y) === 0) grid[y][x] = !grid[y][x]; };
    // The standard penalty: runs, 2x2 blocks, finder-like patterns (with light borders) and balance.
    const penalty = () => {
      let score = 0, dark = 0;
      const add = (len, hist) => { if (hist[0] === 0) len += size; hist.pop(); hist.unshift(len); };
      const count = h => { const n = h[1], core = n > 0 && h[2] === n && h[3] === n * 3 && h[4] === n && h[5] === n;
        return (core && h[0] >= n * 4 && h[6] >= n ? 1 : 0) + (core && h[6] >= n * 4 && h[0] >= n ? 1 : 0); };
      for (const get of [(a, b) => grid[a][b], (a, b) => grid[b][a]]) for (let a = 0; a < size; a++) {
        let color = false, run = 0; const hist = [0, 0, 0, 0, 0, 0, 0];
        for (let b = 0; b < size; b++) {
          if (get(a, b) === color) { run++; if (run === 5) score += 3; else if (run > 5) score++; }
          else { add(run, hist); if (!color) score += count(hist) * 40; color = get(a, b); run = 1; }
        }
        if (color) { add(run, hist); run = 0; }
        add(run + size, hist); score += count(hist) * 40;
      }
      for (let y = 0; y < size - 1; y++) for (let x = 0; x < size - 1; x++) {
        const c = grid[y][x]; if (c === grid[y][x + 1] && c === grid[y + 1][x] && c === grid[y + 1][x + 1]) score += 3; }
      grid.forEach(row => row.forEach(c => { if (c) dark++; }));
      return score + (Math.ceil(Math.abs(dark * 20 - size * size * 10) / (size * size)) - 1) * 10;
    };
    let best = 0, bestScore = Infinity;
    for (let m = 0; m < 8; m++) { apply(m); format(m); const s = penalty(); if (s < bestScore) { best = m; bestScore = s; } apply(m); }
    apply(best); format(best);
    return grid;
  }
'''
