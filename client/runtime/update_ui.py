"""The update notice in the native client (the work is runtime/auto_update.py).

「新版本已准备好」 sits in the header next to 打电话. A click shows what changed first; only
「现在重启更新」 restarts. After an update, what changed is shown once.
"""

JAVASCRIPT = r'''
(() => {
  if (typeof document === 'undefined' || !document.head || typeof window === 'undefined' || window.__oliviaUpdateUi) return;
  window.__oliviaUpdateUi = true;
  const css = document.createElement('style');
  css.textContent = `
  .olivia-update-entry{display:inline-flex;align-items:center;gap:7px;height:32px;padding:0 14px 0 12px;border:1px solid #6f8a5c;
    border-radius:999px;background:#232a20;color:#e4f0d8;font:inherit;font-size:14px;font-weight:600;line-height:20px;
    white-space:nowrap;cursor:pointer;-webkit-app-region:no-drag}
  .olivia-update-entry[hidden]{display:none}
  .olivia-update-entry:hover{background:#2e3829}
  .olivia-update-entry::before{content:"";width:7px;height:7px;border-radius:50%;background:#9fd27c}
  .olivia-update-entry:focus-visible{outline:2px solid #f3eee4;outline-offset:2px}
  .olivia-update{position:fixed;inset:0;z-index:9600;background:rgba(8,8,10,.62);display:flex;align-items:center;
    justify-content:center;font-family:inherit;font-size:14px;line-height:1.65}
  .olivia-update-card{width:min(460px,92vw);max-height:84vh;overflow:auto;padding:26px 24px 22px;border-radius:16px;
    background:#18191c;border:1px solid #2c2d31;color:#f2f0ec;box-shadow:0 24px 64px rgba(0,0,0,.55)}
  .olivia-update-card h2{margin:0 0 4px;font-size:19px;color:#f6f4f0}
  .olivia-update-sub{margin:0 0 14px;color:#b9bbc0}
  .olivia-update-card ul{margin:0 0 16px;padding-left:18px}
  .olivia-update-card li{margin:0 0 8px;color:#d6d4cf}
  .olivia-update-card li b{color:#f6f4f0}
  .olivia-update-note{font-size:12px;color:#8b8d92;margin:0 0 16px}
  .olivia-update-actions{display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap}
  .olivia-update-actions button{height:40px;padding:0 18px;border-radius:999px;border:1px solid rgba(255,255,255,.28);
    background:transparent;color:#f2f0ec;font:inherit;font-weight:600;cursor:pointer}
  .olivia-update-actions button[data-primary]{background:#efe6d6;border-color:#efe6d6;color:#18191c}
  .olivia-update-actions button:disabled{opacity:.6;cursor:default}
  `;
  document.head.append(css);
  let base = null, state = null, dialog = null, shownUpdated = false;
  const node = (tag, cls, text, parent) => { const el = document.createElement(tag); if (cls) el.className = cls; if (text) el.textContent = text; if (parent) parent.append(el); return el; };

  async function local(path, body) {
    const response = await fetch(new URL(path, base), body === undefined ? {} : {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Olivia-Companion-Action': 'confirmed'}, body: JSON.stringify(body)});
    const value = await response.json().catch(() => ({}));
    if (!response.ok || (value.code && value.code !== 200)) throw Error(value?.data?.error_code || 'UPDATE_UNAVAILABLE');
    return value.data;
  }

  const entry = node('button', 'olivia-update-entry', '新版本已准备好');
  entry.type = 'button'; entry.hidden = true; entry.title = '查看这次更新的内容';
  entry.onclick = () => show();
  let placing = false;
  function place() {
    placing = false;
    const group = document.querySelector('[data-olivia-service-buttons]');
    if (group && entry.parentElement !== group) group.prepend(entry);
  }

  function show() {
    if (dialog || !state || !['ready', 'updated'].includes(state.state)) return;
    const ready = state.state === 'ready';
    dialog = node('div', 'olivia-update', null, document.body);
    dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true'); dialog.setAttribute('aria-label', '更新内容');
    const card = node('div', 'olivia-update-card', null, dialog);
    node('h2', null, ready ? `新版本 ${state.version}` : `已更新到 ${state.version}`, card);
    const notes = state.notes || {};
    if (notes.title) node('p', 'olivia-update-sub', notes.title, card);
    if (notes.items?.length) {
      const list = node('ul', null, null, card);
      for (const item of notes.items) {
        const li = node('li', null, null, list);
        if (item.title) node('b', null, item.title, li);
        if (item.title && item.text) li.append('：');
        if (item.text) li.append(item.text);
      }
    }
    if (ready) node('p', 'olivia-update-note', '更新时 Olivia 会重启，大约半分钟，聊天记录、记忆和设置都会保留。选「稍后」的话，下次打开 Olivia 时生效，或在夜里空闲时自动完成。', card);
    const actions = node('div', 'olivia-update-actions', null, card);
    if (ready) {
      const later = node('button', null, '稍后', actions); later.type = 'button'; later.onclick = close;
      const now = node('button', null, '现在重启更新', actions); now.type = 'button'; now.dataset.primary = '';
      now.onclick = async () => {
        now.disabled = later.disabled = true; now.textContent = '正在重启…';
        try { await local('/toy/autoupdate/restart', {}); }
        catch (_) { now.disabled = later.disabled = false; now.textContent = '没能重启，再试一次'; }
      };
      now.focus();
    } else {
      const ok = node('button', null, '知道了', actions); ok.type = 'button'; ok.dataset.primary = '';
      ok.onclick = () => { close(); state = null; local('/toy/autoupdate/seen', {}).catch(() => {}); };
      ok.focus();
    }
    dialog.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  }
  function close() { dialog?.remove(); dialog = null; }

  async function refresh() {
    try { state = await local('/toy/autoupdate/state'); } catch (_) { return; }
    entry.hidden = state.state !== 'ready';
    if (state.state === 'ready') { entry.textContent = '新版本已准备好'; place(); }
    if (state.state === 'updated' && !shownUpdated) { shownUpdated = true; show(); }
  }

  function boot(tries = 0) {
    // Runs from <head>: wait for <body> and the settings marker that carries the backend address.
    if (!document.body) { document.addEventListener('DOMContentLoaded', () => boot(tries), {once: true}); return; }
    const marker = document.querySelector('[data-olivia-companion-settings]');
    base = marker && marker.getAttribute('data-api-base');
    if (!base) { if (tries < 60) setTimeout(() => boot(tries + 1), 1000); return; }
    place();
    new MutationObserver(() => { if (!placing && !entry.hidden) { placing = true; requestAnimationFrame(place); } })
      .observe(document.body, {childList: true, subtree: true});
    refresh(); setInterval(refresh, 60000);
  }
  boot();
})();
'''
