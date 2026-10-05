"""Self-contained script injected into the original Olivia settings surface."""

from __future__ import annotations

import base64
from pathlib import Path


SETTINGS_UI_VERSION = "p03.original-settings-manage.v57"

BOOTSTRAP_JAVASCRIPT = r'''(() => {
  "use strict";

  window.__oliviaReplyFailureMessage = (code, part = "title") => {
    const titles = {
      REPLY_REWRITE_FAILED: "这封回信在修改时遇到了问题，暂时没能寄回。你的信仍保留在信箱里。",
      REPLY_QUALITY_BLOCKED: "这封回信检查后仍有问题，暂时没能寄回。你的信仍保留在信箱里。",
      LLM_TIMEOUT: "这封回信的服务请求超时，暂时没能完成。你的信仍保留在信箱里。",
      LLM_UNAVAILABLE: "回信服务暂时不可用，这封回信还未完成。你的信仍保留在信箱里。"
    };
    if (!Object.hasOwn(titles, code)) return null;
    const transient = code === "LLM_TIMEOUT" || code === "LLM_UNAVAILABLE";
    if (part === "category") return transient ? "transient_reply" : "quality";
    return part === "hint" ? (transient ? "请保留诊断包并稍后查看信件状态，避免重复寄信。" : "请导出诊断包并反馈，修复后可重新寄信。") : titles[code];
  };

  window.__oliviaExplainRelayFailure = payload => {
    const data = payload?.data || payload;
    const messages = {
      OLIVIA_KEY_REQUIRED: "还没有连接 Olivia 账户 Key，请先在设置的“回信服务 → Olivia 账户”获取或导入 Key，再寄信。",
      REPLY_SERVICE_NOT_CONNECTED: "回信服务还没有连上你的 Olivia 账户 Key，请在设置的“回信服务 → Olivia 账户”点“连接并保存”，再寄信。",
      LLM_QUOTA_EXHAUSTED: "Olivia 余额不足，请在“回信服务 → Olivia 账户”充值后再寄信；重复寄信不会恢复余额。",
      LLM_AUTH_FAILED: "大模型服务认证失败，请检查当前账户或 Key 是否有效。",
      LLM_USAGE_PENDING: "这次模型请求中断，用量正在等待核对。请保留诊断包，暂勿反复寄信。",
      LLM_REQUEST_DUPLICATE: "这次请求已经提交，请先查看原信件状态，避免重复寄出。",
      REPLY_REWRITE_FAILED: window.__oliviaReplyFailureMessage("REPLY_REWRITE_FAILED"),
      REPLY_QUALITY_BLOCKED: window.__oliviaReplyFailureMessage("REPLY_QUALITY_BLOCKED"),
      LLM_TIMEOUT: window.__oliviaReplyFailureMessage("LLM_TIMEOUT"),
      LLM_UNAVAILABLE: window.__oliviaReplyFailureMessage("LLM_UNAVAILABLE")
    };
    const message = messages[data?.error_code] || messages[payload?.message];
    if (!message || document.getElementById("olivia-relay-error")) return;
    const box = document.createElement("div");
    box.id = "olivia-relay-error"; box.setAttribute("role", "alert");
    box.style.cssText = "position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:2147483647;max-width:calc(100vw - 48px);padding:16px 20px;border:1px solid #777;border-radius:12px;background:#202124;color:#f3f0e8;box-shadow:0 4px 24px #0008;line-height:1.6";
    const text = document.createElement("span"); text.textContent = message;
    const close = document.createElement("button"); close.type = "button";
    close.textContent = "知道了"; close.style.cssText = "margin-left:16px;padding:4px 12px;cursor:pointer";
    close.addEventListener("click", () => box.remove());
    box.append(text, close); document.body.append(box);
  };

  const loader = document.currentScript;
  const rawApiBase = loader && loader.dataset ? loader.dataset.apiBase : "";
  const ROOT_ATTR = "data-olivia-companion-settings-root";
  const DIALOG_ATTR = "data-olivia-companion-settings-dialog";
  const STATUS_PATH = "/toy/companion/status";
  const MEMORY_PATH = "/toy/companion/memory";
  const PRIVATE_WORLD_PATH = "/toy/companion/private-world";
  const DAILY_LIFE_PATH = PRIVATE_WORLD_PATH + "/life";
  const PROACTIVE_STATUS_PATH = "/toy/proactive/status";
  const PROACTIVE_SETTINGS_PATH = "/toy/proactive/settings";
  const VIDEO_REPLY_SETTINGS_PATH = "/toy/settings/video-reply";
  let refreshVideoReplySetting = async () => {};
  const VIDEO_CAPABILITY_PATH = "/toy/capabilities/video";
  const VIDEO_CAPABILITY_ACTION_PATH = "/toy/capabilities/video/action";
  const DIAGNOSTIC_EXPORT_PATH = "/toy/diagnostics/export";
  const LOCAL_LETTER_IMPORT_PATH = "/toy/letter/legacy/local-import";
  const OFFICIAL_IMPORT_CONFIRM_ATTR = "data-olivia-companion-official-import-confirm";
  const MEMORY_CORRECT_PATH = "/toy/companion/memory/correct";
  const MEMORY_DELETE_PATH = "/toy/companion/memory/delete";
  const MEMORY_CLEAR_PATH = "/toy/companion/memory/clear";
  const MEMORY_PAUSE_PATH = "/toy/companion/memory/pause";
  const MEMORY_RESUME_PATH = "/toy/companion/memory/resume";
  const MEMORY_RETRY_PATH = "/toy/companion/memory/retry";
  const SETUP_STATUS_PATH = "/toy/setup/status";
  const LLM_DELETE_PATH = "/toy/setup/llm/delete";
  const SETUP_COMPLETE_PATH = "/toy/setup/complete";
  const MEM0_CAPABILITY_PATH = "/toy/capabilities/mem0";
  const MEM0_CAPABILITY_ACTION_PATH = "/toy/capabilities/mem0/action";
  const UPDATE_ACTION_PATH = "/toy/updates/local/action";
  const CONFIRM_HEADER = "X-Olivia-Companion-Action";
  const CONFIRM_VALUE = "confirmed";
  const SETUP_CONFIRM_HEADER = "X-Olivia-Setup-Action";
  const SETUP_SESSION_HEADER = "X-Olivia-Setup-Session";
  const CAPABILITY_CONFIRM_HEADER = "X-Olivia-Capability-Action";
  const UPDATE_CONFIRM_HEADER = "X-Olivia-Update-Action";
  const LETTER_CHARACTER_LIMIT = 1200;
  let proactiveState = {
    enabled: false,
    allow_voice: true,
    login_check_enabled: false,
    busy: false,
    remaining: 0,
    reason: "",
    next_check_at: null,
  };
  let proactiveStatusPending = false;
  let proactiveStatusTimer = null;
  const proactiveStateListeners = new Set();
  let mem0RuntimeProgressStartedAt = null;
  const LETTER_COMPOSER_TITLE = "写下你的感受";
  const LETTER_SUBMIT_LABEL = "寄出信件";
  const parseApiBase = (value) => {
    let url;
    try {
      url = new URL(value);
    } catch (_error) {
      return null;
    }
    const loopback = url.hostname === "127.0.0.1" || url.hostname === "localhost";
    if (
      url.protocol !== "http:" ||
      !loopback ||
      !url.port ||
      url.username ||
      url.password ||
      url.search ||
      url.hash ||
      (url.pathname !== "/" && url.pathname !== "")
    ) {
      return null;
    }
    return url;
  };

  const apiBase = parseApiBase(rawApiBase);
  if (!apiBase) {
    return;
  }
  let setupSessionToken = "";
  if (typeof customElements !== 'undefined' && typeof HTMLElement !== 'undefined' && !customElements.get('olivia-photo')) customElements.define('olivia-photo', class extends HTMLElement {
    static get observedAttributes(){return ['letter-id'];}
    connectedCallback(){
      this.parentElement?.classList.add('olivia-photo-stack');
      this.removeAttribute('data-open');
      this.outside=e=>{if(!this.contains(e.target))this.setOpen(false);};
      this.escape=e=>{if(e.key==='Escape')this.setOpen(false);};
      this.refresh();
    }
    disconnectedCallback(){clearTimeout(this.timer);this.controller?.abort();this.setOpen(false);}
    attributeChangedCallback(){if(this.isConnected){this.setOpen(false);this.refresh();}}
    setOpen(open){
      if(open===this.hasAttribute('data-open'))return;
      this.motion?.cancel();
      const before=this.getBoundingClientRect();
      const paper=this.parentElement?.querySelector('.mail-box-reply-content');
      if(open&&paper)this.style.setProperty('--photo-open-height',Math.max(120,paper.clientHeight-48)+'px');
      this.toggleAttribute('data-open',open);
      if(this.isConnected&&this.animate&&!matchMedia('(prefers-reduced-motion: reduce)').matches){
        const after=this.getBoundingClientRect(),base=getComputedStyle(this).transform;
        this.setAttribute('data-moving','');
        this.motion=this.animate([
          {transform:`translate(${before.x-after.x}px,${before.y-after.y}px) scale(${before.width/Math.max(1,after.width)},${before.height/Math.max(1,after.height)}) ${base==='none'?'':base}`},
          {transform:base}
        ],{duration:280,easing:'cubic-bezier(.22,.61,.36,1)'});
        this.motion.onfinish=()=>this.removeAttribute('data-moving');
      }else this.removeAttribute('data-moving');
      const button=this.querySelector('button');
      if(button){button.setAttribute('aria-expanded',String(open));button.setAttribute('aria-label',open?'收起随信照片':'查看随信照片');}
      const caption=this.querySelector('.olivia-letter-photo-print span');
      if(caption)caption.textContent=open?'收起照片':'查看照片';
      document.removeEventListener('pointerdown',this.outside);
      document.removeEventListener('keydown',this.escape);
      if(open){document.addEventListener('pointerdown',this.outside);document.addEventListener('keydown',this.escape);this.ackPhoto();}
    }
    ackPhoto(){
      const img=this.querySelector('img');
      if(!this.isConnected||!this.hasAttribute('data-open')||!img?.naturalWidth)return;
      fetch(new URL('/toy/image/ack',apiBase),{method:'POST',headers:{'Content-Type':'application/json','X-Olivia-Companion-Action':'confirmed'},body:JSON.stringify({filename:new URL(img.src).pathname.split('/').pop()})}).catch(()=>{});
    }
    async refresh(){
      clearTimeout(this.timer);this.controller?.abort();
      const controller=new AbortController();this.controller=controller;
      const id=this.getAttribute('letter-id');if(!id)return;
      try {
        const endpoint=new URL('/toy/image/status',apiBase);endpoint.searchParams.set('letter_id',id);
        const response=await fetch(endpoint,{signal:controller.signal});const raw=await response.json();const data=raw.data;
        if(!response.ok||raw.code!==0||!data?.imageStatus)throw new Error('PHOTO_STATUS_UNAVAILABLE');
        if(controller.signal.aborted||!this.isConnected||id!==this.getAttribute('letter-id'))return;
        if(data?.imageStatus==='COMPLETED' && data.replyImageUrl){
          if(this.dataset.loaded===data.replyImageUrl)return;
          const url=new URL(data.replyImageUrl);if(url.origin!==new URL(apiBase).origin)return;
          const link=document.createElement('button');link.type='button';link.setAttribute('aria-expanded','false');
          link.onclick=e=>{e.stopPropagation();this.setOpen(!this.hasAttribute('data-open'));};
          link.title='查看随信照片';link.setAttribute('aria-label','查看随信照片');link.className='olivia-letter-photo-print';
          const img=document.createElement('img');img.alt='随信照片，点击查看大图';
          const caption=document.createElement('span');caption.textContent='查看照片';
          img.onload=()=>{if(id===this.getAttribute('letter-id'))this.ackPhoto();};
          img.onerror=()=>{if(this.isConnected&&id===this.getAttribute('letter-id')){this.setOpen(false);delete this.dataset.loaded;this.textContent='照片读取暂时中断，正在重试…';this.timer=setTimeout(()=>this.refresh(),5000);}};
          img.src=url.href;link.append(img,caption);this.replaceChildren(link);this.dataset.loaded=url.href;return;
        }
        this.setOpen(false);
        if(['SKIPPED','NOT_REQUESTED'].includes(data?.imageStatus)){this.replaceChildren();return;}
        if(data?.imageStatus==='FAILED'){
          const reasons={GPU_NOT_CONFIGURED:'请先连接 Olivia 账户。',GPU_AUTH_FAILED:'照片服务验证失败，请检查 Olivia 账户。',GPU_INSUFFICIENT_BALANCE:'云端余额不足，请检查云服务余额。',GPU_BILLING_CONSENT_REQUIRED:'请先在设置中确认使用云端服务。',IMAGE_DEPENDENCY_MISSING:'照片组件不完整，请更新或修复客户端。'};
          const code=/^[A-Z][A-Z0-9_]{0,95}$/.test(data.imageErrorCode||'')?data.imageErrorCode:'';
          this.textContent='照片这次没能附上。'+(reasons[code]||'');this.title=code;return;
        }
        this.textContent=data.imageStatus==='RETRY_PENDING'?'照片连接暂时中断，正在自动重试…':data.imagePhase==='waiting'||data.imageCloudStatus==='queued'?'照片正在排队，完成后会附在正文后…':'照片正在准备…';
      } catch(_){if(controller.signal.aborted)return;if(this.isConnected&&id===this.getAttribute('letter-id'))this.textContent='暂时无法获取照片状态，正在重试…';}
      if(this.isConnected)this.timer=setTimeout(()=>this.refresh(),5000);
    }
  });

  const text = (tag, value, className) => {
    const element = document.createElement(tag);
    element.textContent = value;
    if (className) {
      element.className = className;
    }
    return element;
  };

  const button = (label, onClick) => {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = label;
    element.className = "px-6 py-2.5 rounded-full border border-grey-5 text-text-body text-label-m font-medium cursor-pointer hover:bg-surface-1 transition-colors";
    element.style.pointerEvents = "auto";
    element.style.webkitAppRegion = "no-drag";
    element.addEventListener("click", onClick);
    return element;
  };

  const setButtonsBusy = (buttons, busy) => {
    for (const item of buttons) {
      item.disabled = busy;
      item.style.opacity = busy ? "0.55" : "1";
      item.style.cursor = busy ? "default" : "pointer";
    }
  };

  const confirmAction = (message) => new Promise((resolve) => {
    document.querySelector(`[${OFFICIAL_IMPORT_CONFIRM_ATTR}]`)?.remove();
    const backdrop = document.createElement("div");
    backdrop.setAttribute(OFFICIAL_IMPORT_CONFIRM_ATTR, "");
    backdrop.style.position = "fixed";
    backdrop.style.inset = "0";
    backdrop.style.zIndex = "2147483000";
    backdrop.style.display = "grid";
    backdrop.style.placeItems = "center";
    backdrop.style.padding = "24px";
    backdrop.style.backgroundColor = "rgba(0, 0, 0, 0.72)";
    backdrop.style.pointerEvents = "auto";
    backdrop.style.webkitAppRegion = "no-drag";

    const confirmation = document.createElement("section");
    confirmation.setAttribute("role", "dialog");
    confirmation.setAttribute("aria-modal", "true");
    confirmation.setAttribute("aria-labelledby", "olivia-companion-confirm-message");
    confirmation.style.width = "min(520px, calc(100vw - 48px))";
    confirmation.style.padding = "24px";
    confirmation.style.borderRadius = "12px";
    confirmation.style.backgroundColor = "#18191c";
    confirmation.style.color = "#f9fafb";
    confirmation.style.colorScheme = "dark";
    confirmation.style.boxShadow = "0 24px 80px rgba(0, 0, 0, 0.45)";
    confirmation.style.pointerEvents = "auto";
    confirmation.style.webkitAppRegion = "no-drag";

    const finish = (accepted) => {
      backdrop.remove();
      resolve(accepted);
    };
    const messageNode = text("p", message, "text-text-body text-body-m font-regular");
    messageNode.id = "olivia-companion-confirm-message";
    messageNode.style.color = "#f9fafb";
    const actionsNode = actions();
    actionsNode.style.justifyContent = "flex-end";
    const cancel = button("取消", () => finish(false));
    const confirm = button("确定", () => finish(true));
    for (const item of [cancel, confirm]) {
      item.style.color = "#f9fafb";
      item.style.backgroundColor = "#111827";
      item.style.borderColor = "#6b7280";
      item.style.pointerEvents = "auto";
      item.style.webkitAppRegion = "no-drag";
    }
    confirm.style.backgroundColor = "#2563eb";
    confirm.style.color = "#ffffff";
    actionsNode.append(cancel, confirm);
    confirmation.append(messageNode, actionsNode);
    backdrop.append(confirmation);
    backdrop.addEventListener("click", (event) => {
      if (event.target === backdrop) {
        event.preventDefault();
        finish(false);
      }
    });
    backdrop.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        finish(false);
      }
    });
    (document.body || document.documentElement).append(backdrop);
    confirm.focus();
  });

  const card = () => {
    const element = document.createElement("article");
    element.style.padding = "14px";
    element.style.borderRadius = "10px";
    element.style.background = "#2b2e35";
    element.style.display = "grid";
    element.style.gap = "8px";
    return element;
  };

  const stack = () => {
    const element = document.createElement("div");
    element.style.display = "grid";
    element.style.gap = "10px";
    return element;
  };

  const actions = () => {
    const element = document.createElement("div");
    element.style.display = "flex";
    element.style.flexWrap = "wrap";
    element.style.gap = "8px";
    element.style.alignItems = "center";
    return element;
  };

  const field = (label, value) => {
    const row = document.createElement("div");
    row.style.display = "grid";
    row.style.gridTemplateColumns = "minmax(110px, 0.7fr) minmax(0, 1.3fr)";
    row.style.gap = "12px";
    row.append(
      text("span", label, "text-text-secondary text-body-m font-regular"),
      text("span", value, "text-text-body text-body-m font-medium")
    );
    return row;
  };

  const formatTime = (value) => {
    if (typeof value !== "string" || !value) {
      return "";
    }
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) {
      return "";
    }
    try {
      return parsed.toLocaleString("zh-CN", { hour12: false });
    } catch (_error) {
      return value;
    }
  };

  const stateLabels = {
    available: "可用",
    degraded: "部分可用",
    unavailable: "暂不可用",
    disabled: "未启用",
  };

  const capabilityState = (value) => {
    const state = value && typeof value.state === "string" ? value.state : "unavailable";
    return Object.hasOwn(stateLabels, state) ? state : "unavailable";
  };

  const privateWorldState = (value) => {
    const state = value && typeof value.state === "string" ? value.state : "unavailable";
    return state === "available" || state === "disabled" || state === "unavailable"
      ? state
      : "unavailable";
  };

  const requestId = (prefix) => {
    let token = "";
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      token = window.crypto.randomUUID();
    } else {
      token = `${Date.now().toString(36)}.${Math.random().toString(36).slice(2)}`;
    }
    return `${prefix}.${token}`
      .replace(/[^A-Za-z0-9._:-]/g, ".")
      .slice(0, 160);
  };
  const videoReplyRequestId = () => requestId("video_reply_setting").replace("video_reply_setting.", "video_reply_setting:");

  const requestJson = async (path, params = {}) => {
    const endpoint = new URL(path, apiBase);
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== "") {
        endpoint.searchParams.set(key, String(value));
      }
    }
    const controller = new AbortController();
    const timeoutMs = path === VIDEO_CAPABILITY_PATH || path === VIDEO_REPLY_SETTINGS_PATH
      ? 300000
      : path === MEMORY_PATH ? 45000
      : path === STATUS_PATH || path === PROACTIVE_STATUS_PATH ? 15000 : 5000;
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(endpoint, {
        method: "GET",
        cache: "no-store",
        credentials: "omit",
        headers: { "Accept": "application/json" },
        signal: controller.signal,
      });
      const responseBody = await response.json();
      const payload = (path === VIDEO_REPLY_SETTINGS_PATH
          || path === LOCAL_LETTER_IMPORT_PATH
          || path === PROACTIVE_STATUS_PATH)
        && responseBody && responseBody.data
        ? responseBody.data
        : responseBody;
      const valid = path === LOCAL_LETTER_IMPORT_PATH
        ? params.relationship === "1"
          ? payload && ["PENDING", "RUNNING", "APPLIED", "FAILED", "UNAVAILABLE"].includes(payload.status)
          : params.progress === "1"
          ? payload && ["IDLE", "RUNNING", "APPLIED", "FAILED", "UNAVAILABLE"].includes(payload.status)
          : payload && payload.status === "READY"
          && Number.isInteger(payload.seen)
          && Number.isInteger(payload.would_insert)
          && Number.isInteger(payload.would_update)
          && Number.isInteger(payload.would_remove)
          && Number.isInteger(payload.duplicates)
        : path === VIDEO_REPLY_SETTINGS_PATH
        ? payload && (payload.state === "available" && typeof payload.enabled === "boolean"
          || payload.state === "unavailable" && typeof payload.reason_code === "string")
        : path === PROACTIVE_STATUS_PATH
        ? payload
          && typeof payload.enabled === "boolean"
          && typeof payload.allow_voice === "boolean"
          && typeof payload.login_check_enabled === "boolean"
          && typeof payload.busy === "boolean"
          && Number.isInteger(payload.remaining)
          && typeof payload.reason === "string"
        : payload && ["READY", "PAUSED", "UNAVAILABLE"].includes(payload.status);
      if (!response.ok || !valid) {
        const error = new Error("unavailable");
        error.code = payload && typeof payload.error_code === "string"
          ? payload.error_code
          : "COMPANION_READ_UNAVAILABLE";
        throw error;
      }
      return payload;
    } finally {
      window.clearTimeout(timeout);
    }
  };

  const publishProactiveState = (payload) => {
    proactiveState = {
      ...proactiveState,
      enabled: payload.enabled === true,
      allow_voice: payload.allow_voice !== false,
      login_check_enabled: payload.login_check_enabled === true,
      busy: payload.busy === true,
      remaining: Number.isInteger(payload.remaining) && payload.remaining >= 0 ? payload.remaining : 0,
      reason: typeof payload.reason === "string" ? payload.reason : "",
      next_check_at: Number.isFinite(payload.next_check_at) ? payload.next_check_at : null,
    };
    for (const listener of proactiveStateListeners) {
      try { listener(proactiveState); } catch (_error) { /* one view cannot break the poll */ }
    }
    try {
      const event = new Event("olivia-proactive-status");
      event.proactiveState = proactiveState;
      window.dispatchEvent(event);
    } catch (_error) { /* older CEF may not expose Event constructors */ }
  };

  const refreshProactiveStatus = async () => {
    if (proactiveStatusPending || !apiBase) return proactiveState;
    proactiveStatusPending = true;
    try {
      publishProactiveState(await requestJson(PROACTIVE_STATUS_PATH));
    } catch (_error) {
      publishProactiveState({ ...proactiveState, busy: false, reason: "PROACTIVE_STATUS_UNAVAILABLE" });
    } finally {
      proactiveStatusPending = false;
    }
    return proactiveState;
  };

  const saveProactiveSettings = async (body) => {
    const endpoint = new URL(PROACTIVE_SETTINGS_PATH, apiBase);
    const response = await fetch(endpoint, {
      method: "POST",
      cache: "no-store",
      credentials: "omit",
      headers: { "Accept": "application/json", "Content-Type": "application/json", [CONFIRM_HEADER]: CONFIRM_VALUE },
      body: JSON.stringify({
        enabled: body.enabled === true,
        allow_voice: body.allow_voice !== false,
        login_check_enabled: body.login_check_enabled === true,
      }),
    });
    let responseBody = null;
    try { responseBody = await response.json(); } catch (_error) { /* malformed response */ }
    const payload = responseBody && responseBody.data && typeof responseBody.data === "object"
      ? responseBody.data : responseBody;
    if (!response.ok || (responseBody?.code != null && responseBody.code !== 0) || !payload || typeof payload.enabled !== "boolean") {
      const error = new Error("PROACTIVE_SETTINGS_UNAVAILABLE");
      error.code = payload && typeof payload.error_code === "string"
        ? payload.error_code : "PROACTIVE_SETTINGS_UNAVAILABLE";
      throw error;
    }
    publishProactiveState(payload);
    return payload;
  };

  const requestDiagnosticExport = async () => {
    const endpoint = new URL(DIAGNOSTIC_EXPORT_PATH, apiBase);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(endpoint, {
        method: "GET",
        cache: "no-store",
        credentials: "omit",
        headers: { "Accept": "application/zip" },
        signal: controller.signal,
      });
      if (!response.ok) {
        let payload = null;
        try {
          payload = await response.json();
        } catch (_error) {
          payload = null;
        }
        const error = new Error("diagnostic-export-unavailable");
        error.code = payload && typeof payload.error_code === "string"
          ? payload.error_code
          : "DIAGNOSTIC_EXPORT_UNAVAILABLE";
        throw error;
      }
      const blob = await response.blob();
      if (!blob || blob.size < 1) {
        const error = new Error("diagnostic-export-empty");
        error.code = "DIAGNOSTIC_EXPORT_UNAVAILABLE";
        throw error;
      }
      return blob;
    } finally {
      window.clearTimeout(timeout);
    }
  };

  const requestMutation = async (path, body) => {
    const endpoint = new URL(path, apiBase);
    const controller = new AbortController();
    const timeoutMs = (
      path === VIDEO_REPLY_SETTINGS_PATH
      || path === LOCAL_LETTER_IMPORT_PATH
      || path === MEMORY_CLEAR_PATH
      || path.startsWith("/toy/letter/backup/")
      || path.startsWith("/toy/letter/maintenance/")
    )
      ? 300000
      : 8000;
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        cache: "no-store",
        credentials: "omit",
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/json",
          [CONFIRM_HEADER]: CONFIRM_VALUE,
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      let responseBody = null;
      try {
        responseBody = await response.json();
      } catch (_error) {
        responseBody = null;
      }
      const payload = (path === VIDEO_REPLY_SETTINGS_PATH
          || path === LOCAL_LETTER_IMPORT_PATH
          || path === MEMORY_RETRY_PATH || path.startsWith("/toy/letter/backup/")
          || path.startsWith("/toy/letter/maintenance/"))
        && responseBody && responseBody.data && typeof responseBody.data === "object"
        ? responseBody.data
        : responseBody;
      if (!response.ok || !payload || typeof payload.status !== "string") {
        const error = new Error("mutation-unavailable");
        error.code = payload && typeof payload.error_code === "string"
          ? payload.error_code
          : "COMPANION_MUTATION_UNAVAILABLE";
        error.missingDependencies = payload && Array.isArray(payload.missing_dependencies)
          ? payload.missing_dependencies.filter((item) => typeof item === "string")
          : [];
        throw error;
      }
      return payload;
    } finally {
      window.clearTimeout(timeout);
    }
  };

  const requestSetup = async (path, body = null) => {
    if ((path === "/toy/relay/action" || path === "/toy/generation/action") && !setupSessionToken) {
      await requestSetup(SETUP_STATUS_PATH);
    }
    const endpoint = new URL(path, apiBase);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), path === "/toy/cloud/action" ? 45000 : 25000);
    const options = {
      cache: "no-store",
      credentials: "omit",
      headers: { "Accept": "application/json" },
      signal: controller.signal,
    };
    if (body !== null) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.headers[SETUP_CONFIRM_HEADER] = CONFIRM_VALUE;
      options.headers[SETUP_SESSION_HEADER] = setupSessionToken;
      options.body = JSON.stringify(body);
    }
    try {
      const response = await fetch(endpoint, options);
      let payload = null;
      try {
        payload = await response.json();
      } catch (_error) {
        payload = null;
      }
      if (!response.ok || !payload || (path !== "/toy/relay/action" && typeof payload.status !== "string")) {
        const error = new Error("setup-unavailable");
        error.code = payload && typeof payload.error_code === "string"
          ? payload.error_code
          : "LLM_SETUP_UNAVAILABLE";
        throw error;
      }
      if (
        path === SETUP_STATUS_PATH
        && typeof payload.session_token === "string"
        && payload.session_token.length >= 32
      ) {
        setupSessionToken = payload.session_token;
      }
      return payload;
    } finally {
      window.clearTimeout(timeout);
    }
  };

  const requestCapability = async (path, body = null, timeoutMs = 25000) => {
    if (body !== null && !setupSessionToken) {
      await requestSetup(SETUP_STATUS_PATH);
    }
    const endpoint = new URL(path, apiBase);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    const options = {
      cache: "no-store",
      credentials: "omit",
      headers: { "Accept": "application/json" },
      signal: controller.signal,
    };
    if (body !== null) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.headers[CAPABILITY_CONFIRM_HEADER] = CONFIRM_VALUE;
      options.headers[SETUP_SESSION_HEADER] = setupSessionToken;
      options.body = JSON.stringify(body);
    }
    try {
      const response = await fetch(endpoint, options);
      const payload = await response.json();
      if (
        !response.ok
        || !payload
        || typeof payload.status !== "string"
        || (path === MEM0_CAPABILITY_PATH && payload.capability !== "long_term_memory")
      ) {
        const error = new Error("capability-unavailable");
        error.code = payload && typeof payload.error_code === "string"
          && /^[A-Z][A-Z0-9_]{0,95}$/.test(payload.error_code)
          ? payload.error_code : "CAPABILITY_UNAVAILABLE";
        throw error;
      }
      return payload;
    } finally {
      window.clearTimeout(timeout);
    }
  };

  const requestUpdate = async (body) => {
    if (!setupSessionToken) {
      await requestSetup(SETUP_STATUS_PATH);
    }
    const endpoint = new URL(UPDATE_ACTION_PATH, apiBase);
    const controller = new AbortController();
    const timeoutMs = body.action === "select" ? 310000 : 120000;
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        cache: "no-store",
        credentials: "omit",
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/json",
          [UPDATE_CONFIRM_HEADER]: CONFIRM_VALUE,
          [SETUP_SESSION_HEADER]: setupSessionToken,
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      const payload = await response.json();
      if (!response.ok || !payload || typeof payload.status !== "string") {
        const error = new Error("update-unavailable");
        error.code = payload && typeof payload.error_code === "string"
          ? payload.error_code
          : "UPDATE_ACTION_UNAVAILABLE";
        throw error;
      }
      return payload;
    } finally {
      window.clearTimeout(timeout);
    }
  };

  const setDiagnosticDetails = (target, values) => {
    const codes = [...new Set((Array.isArray(values) ? values : [values])
      .filter(value => typeof value === "string" && /^[A-Z][A-Z0-9_]{0,95}$/.test(value)))].slice(0, 5);
    let details = target.__oliviaDiagnosticDetails;
    if (!details && !codes.length) return;
    if (!details) {
      details = document.createElement("details");
      details.__oliviaCodeNode = text("p", "");
      details.append(text("summary", "诊断详情"), details.__oliviaCodeNode);
      target.__oliviaDiagnosticDetails = details;
    }
    details.hidden = !codes.length;
    details.__oliviaCodeNode.textContent = codes.join(" · ");
    if (target.parentElement && details.parentElement !== target.parentElement) {
      target.parentElement.append(details);
    }
  };

  const memoryClearFailureMessage = (error) => {
    if (error && error.code === "MEMORY_ADMIN_BUSY") {
      return "正在导入或写入记忆，请完成后再清空；本次未执行清空。";
    }
    return "长期记忆清空未完成，原始信件和林离世界保持不变。请稍后重试。";
  };

  const mutationMessage = (payload, appliedText) => {
    if (!payload || typeof payload.status !== "string") {
      return "操作结果无法确认。";
    }
    if (payload.status === "APPLIED") {
      return appliedText;
    }
    if (payload.status === "DUPLICATE") {
      return "该操作已经完成。";
    }
    if (payload.status === "NOOP") {
      return "没有需要修改的内容。";
    }
    return "操作未执行，请刷新后重试。";
  };

  const renderUnavailable = (panel, state, label) => {
    panel.replaceChildren(
      text("h3", label, "text-text-title text-title-m"),
      text(
        "p",
        `${label}${state === "disabled" ? "未启用。" : "暂时不可用。"}`,
        "text-text-secondary text-body-m font-regular"
      )
    );
  };

  const renderMemories = (list, memories, reload, resultState) => {
    list.replaceChildren();
    if (!Array.isArray(memories) || memories.length === 0) {
      list.append(
        text("p", "暂无提取记忆。历史信件原文不计入此列表。", "text-text-secondary text-body-m font-regular")
      );
      return;
    }
    for (const memory of memories) {
      if (!memory || typeof memory.text !== "string") {
        continue;
      }
      const item = card();
      const memoryText = text(
        "p",
        memory.text,
        "text-text-body text-body-m font-regular"
      );
      item.append(memoryText);
      const created = memory.created_at == null ? "时间未知" : formatTime(memory.created_at);
      if (created) {
        item.append(
          text("p", created, "text-text-secondary text-caption-m font-regular")
        );
      }

      if (typeof memory.memory_id === "string" && memory.memory_id) {
        const controls = actions();
        let editor = null;
        const correct = button("纠正", () => {
          if (editor) {
            editor.querySelector("textarea")?.focus();
            return;
          }
          editor = stack();
          const input = document.createElement("textarea");
          input.value = memory.text;
          input.maxLength = 2000;
          input.rows = 4;
          input.setAttribute("aria-label", "正确的长期记忆内容");
          input.className = "w-full rounded-3 border border-grey-5 bg-transparent px-4 py-3 text-text-body text-body-m";

          const editorActions = actions();
          const save = button("保存更正", async () => {
            const replacement = input.value.trim();
            if (!replacement) {
              resultState.textContent = "正确内容不能为空。";
              input.focus();
              return;
            }
            if (replacement === memory.text.trim()) {
              resultState.textContent = "内容没有变化。";
              return;
            }
            if (!await confirmAction("确认用新内容替换这条长期记忆？")) {
              return;
            }
            setButtonsBusy([save, cancel, correct, remove], true);
            resultState.textContent = "正在更正长期记忆……";
            try {
              const payload = await requestMutation(MEMORY_CORRECT_PATH, {
                memory_id: memory.memory_id,
                replacement_text: replacement,
                request_id: requestId("memory.correct"),
                reason: "用户在原版 Olivia 设置中明确纠正长期记忆。",
              });
              await reload();
              resultState.textContent = mutationMessage(payload, "长期记忆已更正。");
            } catch (_error) {
              resultState.textContent = "长期记忆更正失败，原记录保持不变。";
            } finally {
              setButtonsBusy([save, cancel, correct, remove], false);
            }
          });
          const cancel = button("取消", () => {
            editor?.remove();
            editor = null;
            correct.focus();
          });
          editorActions.append(save, cancel);
          editor.append(
            text(
              "p",
              "先写入正确事实，确认成功后再删除旧事实。",
              "text-text-secondary text-caption-m font-regular"
            ),
            input,
            editorActions
          );
          item.append(editor);
          input.focus();
        });

        const remove = button("删除", async () => {
          if (!await confirmAction("确认删除这条长期记忆？原始信件不会被删除。")) {
            return;
          }
          setButtonsBusy([correct, remove], true);
          resultState.textContent = "正在删除长期记忆……";
          try {
            const payload = await requestMutation(MEMORY_DELETE_PATH, {
              memory_id: memory.memory_id,
              request_id: requestId("memory.delete"),
              reason: "用户在原版 Olivia 设置中明确删除长期记忆。",
            });
            await reload();
            resultState.textContent = mutationMessage(payload, "长期记忆已删除。");
          } catch (_error) {
            resultState.textContent = "长期记忆删除失败，原记录保持不变。";
          } finally {
            setButtonsBusy([correct, remove], false);
          }
        });
        const more = document.createElement("details");
        more.className = "olivia-memory-more";
        more.append(text("summary", "更多"), remove);
        controls.className += " om-record-actions";
        controls.append(correct, more);
        item.append(controls);
      }
      list.append(item);
    }
    if (!list.childElementCount) {
      list.append(
        text("p", "暂无可显示的长期记忆。", "text-text-secondary text-body-m font-regular")
      );
    }
  };

  const renderCompanionStatus = (statusNode, capabilities) => {
    if (!statusNode) return;
    statusNode.textContent = "本机陪伴服务已连接。";
    setDiagnosticDetails(statusNode, []);
    statusNode.dataset.state = "available";
    const failed = Object.entries(capabilities).filter(([, value]) =>
      value && (value.state === "unavailable" || value.state === "degraded"));
    if (failed.length) {
      const labels = {memory: "长期记忆", private_world: "林离世界", candidates: "记忆候选"};
      statusNode.textContent = "本机陪伴服务已连接；" + failed.map(([name, value]) => {
        return `${labels[name] || "部分功能"}尚未准备好`;
      }).join("；") + "。";
      setDiagnosticDetails(statusNode, failed.map(([, value]) => value.reason_code));
      statusNode.dataset.state = "degraded";
    }
  };

  const scheduleMemoryStatusRefresh = (panel, delay = 1000) => {
    window.clearTimeout(panel.__oliviaMemoryStatusTimer);
    panel.__oliviaMemoryStatusTimer = window.setTimeout(async () => {
      if (!panel.isConnected) return;
      try {
        const status = await requestJson(STATUS_PATH);
        renderCompanionStatus(panel.__oliviaCompanionStatusNode, status.capabilities);
        await renderMemoryPanel(panel, status.capabilities.memory);
      } catch (_error) {
        scheduleMemoryStatusRefresh(panel, Math.min(delay * 2, 5000));
      }
    }, delay);
  };

  // THESIS: Accumulated memories stay browsable in a fixed-height local browser.
  // OWN-WORLD: Olivia dark surfaces, warm white text, restrained separators.
  // STORY: Search the archive, select a record, inspect its source, correct it.
  // FIRST VIEWPORT: Desktop list/detail columns; fixed paging and record actions.
  // FORM: User-approved HTML prototype; narrow windows switch list/detail views.
  // FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, and DESIGN.md
  const createMemoryBrowser = (panel, capability, summary, resultState, updateSummary) => {
    const el = (tag, className) => {
      const node = document.createElement(tag); node.className = className; return node;
    };
    const root = el("div", "om-browser");
    root.setAttribute("data-olivia-memory-browser", "");
    const style = document.createElement("style");
    style.textContent = `
      [data-olivia-memory-browser]{display:flex;flex-direction:column;min-width:0;min-height:0;height:100%;position:relative;color:#e8e3db;font-size:14px}
      [data-olivia-memory-browser] *{box-sizing:border-box}
      [data-olivia-memory-browser] [hidden]{display:none!important}
      [data-olivia-memory-browser] p{margin:0;line-height:1.8}
      [data-olivia-memory-browser] button,[data-olivia-memory-browser] input,[data-olivia-memory-browser] select,[data-olivia-memory-browser] textarea{font:inherit;border-radius:8px!important;background:#1a1b1d!important;border:1px solid #4c5055!important;color:#e8e3db!important;padding:8px 12px;min-width:0}
      [data-olivia-memory-browser] button{cursor:pointer}
      [data-olivia-memory-browser] button:disabled{opacity:.45;cursor:default}
      [data-olivia-memory-browser] button:hover:not(:disabled){background:#2b2e31!important}
      [data-olivia-memory-browser] :focus-visible{outline:2px solid #dfc99f;outline-offset:3px}
      .om-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:20px;flex-shrink:0}
      .om-heading p{font-size:13px;color:#b3b5b8}
      .om-shell{display:flex;flex-direction:column;flex:1;min-width:0;min-height:0;border-radius:12px;background:#1a1b1d;overflow:hidden}
      .om-toolbar{padding:0 20px 16px;border-bottom:1px solid #383b3e;flex-shrink:0}
      .om-tabs{display:flex;gap:24px;margin-bottom:14px}
      [data-olivia-memory-browser] .om-tab{border:0!important;border-bottom:2px solid transparent!important;border-radius:0!important;padding:14px 0 10px!important;background:transparent!important;color:#b3b5b8!important}
      [data-olivia-memory-browser] .om-tab[aria-selected="true"]{border-bottom-color:#dbd2c5!important;color:#e8e3db!important}
      .om-tools{display:flex;gap:10px;align-items:center}
      .om-search{display:flex;flex:1;min-width:0}
      [data-olivia-memory-browser] .om-search input{width:100%;background:#111213!important}
      [data-olivia-memory-browser] input::placeholder{color:#b3b5b8}
      .om-filter{display:flex;align-items:center;gap:6px;min-width:0;color:#b3b5b8;font-size:12px;white-space:nowrap}
      .om-source-filter{display:flex;align-items:center;gap:12px;margin-top:10px;font-size:12px;color:#b3b5b8}
      .om-workspace{display:grid;grid-template-columns:minmax(240px,.84fr) minmax(0,1.16fr);flex:1;min-width:0;min-height:0}
      .om-list-panel{display:flex;flex-direction:column;min-width:0;min-height:0;border-right:1px solid #383b3e}
      .om-count{font-size:12px;color:#b3b5b8;padding:12px 20px;flex-shrink:0}
      .om-rows{overflow-y:auto;min-height:0;flex:1;padding:0 10px;scrollbar-width:thin;scrollbar-color:#54585c transparent}
      [data-olivia-memory-browser] .om-row{display:block;width:100%;text-align:left;border:0!important;border-radius:8px!important;padding:12px!important;margin:2px 0;border-bottom:1px solid #303337!important;background:transparent!important}
      [data-olivia-memory-browser] .om-row[aria-current="true"]{background:#2c3031!important}
      .om-row-title{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600;font-size:14px}
      .om-row-excerpt{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;color:#b3b5b8;font-size:12px;line-height:1.6;margin-top:4px}
      .om-row-time{display:block;color:#a4a9af;font-size:11px;margin-top:6px}
      .om-pagination{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:12px 16px;border-top:1px solid #383b3e;flex-shrink:0;font-size:12px;color:#b3b5b8}
      [data-olivia-memory-browser] .om-pagination button{padding:5px 8px;font-size:12px}
      .om-detail{display:flex;flex-direction:column;overflow:hidden;min-width:0;min-height:0}
      .om-detail-scroll{flex:1;min-height:0;overflow-y:auto;padding:24px 28px;scrollbar-width:thin}
      .om-detail-footer{flex-shrink:0;border-top:1px solid #383b3e;padding:16px 28px}
      .om-detail-footer>div{display:flex;flex-wrap:wrap;gap:10px}
      .om-detail-footer .olivia-memory-more>button{top:auto;bottom:48px}
      .om-detail h3{font-size:21px;line-height:1.6;margin:0 0 18px;overflow-wrap:anywhere}
      [data-olivia-memory-browser] .om-detail article{padding:0!important;background:transparent!important;border:0!important;margin:0!important}
      [data-olivia-memory-browser] .om-detail article>p:first-child{font-size:16px;line-height:1.95;white-space:pre-wrap;overflow-wrap:anywhere;margin:0 0 20px}
      [data-olivia-memory-browser] .om-detail article>p:not(:first-child){font-size:12px;color:#b3b5b8}
      .om-detail article>div{display:flex;flex-wrap:wrap;gap:10px;margin-top:20px}
      .om-detail article>div:has(textarea){display:grid;grid-template-columns:minmax(0,1fr)}
      .om-detail textarea{width:100%;min-height:140px;resize:vertical;line-height:1.8}
      .om-detail article>div:has(textarea)>div{display:flex;gap:10px}
      .om-original-text{white-space:pre-wrap;overflow-wrap:anywhere;font-size:16px;line-height:1.95;margin:20px 0!important}
      .om-provenance{padding-top:20px;border-top:1px solid #383b3e;margin-top:24px;color:#b3b5b8;font-size:12px}
      .om-result{flex-shrink:0;color:#b3b5b8;font-size:12px;padding-top:10px;min-height:24px}
      .om-management{position:absolute;right:0;top:44px;z-index:4;background:#232527;border-radius:12px;padding:22px;width:min(430px,100%);max-height:calc(100% - 44px);overflow:auto;box-shadow:0 12px 40px #0007}
      .om-management h3{font-size:20px;margin:0 0 15px}
      .om-management-controls{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}
      .om-management p{font-size:12px;color:#b3b5b8}
      .olivia-memory-more{position:relative}
      .olivia-memory-more>summary{cursor:pointer;list-style:none;padding:8px 12px;border:1px solid #4c5055;border-radius:8px}
      .olivia-memory-more>button{position:absolute;top:48px;right:0;min-width:100px;z-index:2;color:#ecaaa2!important;background:#282b2e!important}
      .om-empty{color:#b3b5b8;font-size:14px;padding:30px 12px}
      [data-olivia-memory-browser] .om-back{display:none}
      @media(max-width:700px){
        .om-tools{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}
        .om-search{grid-column:1/-1}.om-filter select{flex:1}
        .om-tools>button{grid-column:1/-1;justify-self:start}
        .om-workspace{display:flex;flex-direction:column}.om-list-panel{flex:1;border-right:0}.om-detail{display:none}
        [data-olivia-memory-browser][data-reading="true"] .om-list-panel{display:none}
        [data-olivia-memory-browser][data-reading="true"] .om-toolbar{display:none}
        [data-olivia-memory-browser][data-reading="true"] .om-detail{display:flex;flex:1}
        [data-olivia-memory-browser] .om-back{display:block;align-self:flex-start;flex-shrink:0;margin:20px 20px 0}
        .om-detail-scroll{padding:20px}.om-detail-footer{padding:14px 20px}
        .om-toolbar{padding:0 14px 14px}
      }`.replaceAll("[data-olivia-memory-browser]", "[data-olivia-memory-dialog] [data-olivia-memory-browser]");
    const state = panel.__oliviaMemoryBrowserState || {
      active: "memories",
      memories: {query: "", page: 1, days: 0, sort: "new", selected: null, scroll: 0},
      originals: {query: "", page: 1, days: 0, sort: "new", selected: null, scroll: 0, source_id: null},
    };
    panel.__oliviaMemoryBrowserState = state;
    let generation = 0;
    const heading = el("div", "om-heading"), management = el("aside", "om-management");
    management.hidden = true; management.setAttribute("aria-label", "记忆管理");
    const originalProgress = text("p", "展开管理后可刷新信件接入进度。");
    const managementControls = el("div", "om-management-controls");
    const manage = button("记忆管理", async () => {
      management.hidden = !management.hidden; manage.setAttribute("aria-expanded", String(!management.hidden));
      if (management.hidden || capability?.reason_code === "MEMORY_ADMIN_PAUSED") return;
      try {
        const payload = await requestJson(MEMORY_PATH, {browse: 1, collection: "originals", limit: 1});
        originalProgress.textContent = "已接入 " + payload.indexed_letters + " 封信。" +
          (Number.isInteger(payload.archive_total) ? "历史信件 " + payload.archive_indexed + "/" + payload.archive_total + " 封。" : "历史信件总量尚未确定。");
      } catch (_) { originalProgress.textContent = "接入进度暂时无法读取，可关闭管理后重新打开。"; }
    });
    manage.setAttribute("aria-expanded", "false");
    const closeManagement = button("收起管理", () => { management.hidden = true; manage.setAttribute("aria-expanded", "false"); manage.focus(); });
    management.append(text("h3", "记忆管理"), originalProgress, managementControls, closeManagement);
    heading.append(summary, manage);
    const shell = el("div", "om-shell"), toolbar = el("div", "om-toolbar"), tabs = el("div", "om-tabs");
    tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-label", "浏览内容");
    const tools = el("div", "om-tools"), searchWrap = el("label", "om-search");
    const input = document.createElement("input"); input.type = "search"; input.maxLength = 500;
    searchWrap.append(input);
    const select = (label, values) => {
      const wrapper = el("label", "om-filter"), control = document.createElement("select");
      control.setAttribute("aria-label", label);
      for (const [value, name] of values) { const option = text("option", name); option.value = value; control.append(option); }
      wrapper.append(text("span", label), control); tools.append(wrapper); return control;
    };
    tools.append(searchWrap);
    const days = select("时间", [["0","全部时间"],["7","最近 7 天"],["30","最近 30 天"],["90","最近 90 天"]]);
    const sort = select("排序", [["new","最近记录"],["old","最早记录"]]);
    const sourceFilter = el("div", "om-source-filter");
    sourceFilter.append(text("span", "正在查看关联原文"), button("显示全部原文", () => { state.originals.source_id = null; state.originals.page = 1; load(); }));
    const workspace = el("div", "om-workspace"), listPanel = el("section", "om-list-panel");
    const count = text("p", "", "om-count"), list = el("div", "om-rows"), pagination = el("nav", "om-pagination");
    list.setAttribute("aria-label", "记忆与原文列表"); pagination.setAttribute("aria-label", "分页");
    const detail = el("section", "om-detail"); detail.setAttribute("aria-label", "选中条目的详情");
    const pageLabel = text("span", "");
    const saveScroll = () => { state[state.active].scroll = list.scrollTop || 0; };
    const previous = button("上一页", () => { saveScroll(); state[state.active].page--; state[state.active].scroll = 0; load(); });
    const next = button("下一页", () => { saveScroll(); state[state.active].page++; state[state.active].scroll = 0; load(); });
    const key = row => state.active === "memories" ? row.memory_id : row.source_id + ":" + row.speaker;
    const selectRow = row => {
      state[state.active].selected = key(row);
      for (const item of list.querySelectorAll("button")) item.setAttribute("aria-current", String(item.dataset.memoryKey === key(row)));
      detail.replaceChildren(button("返回列表", () => {
        root.setAttribute("data-reading", "false");
        Array.from(list.querySelectorAll("button")).find(item => item.dataset.memoryKey === state[state.active].selected)?.focus();
      }));
      detail.children[0].className = "om-back";
      const reading = el("div", "om-detail-scroll");
      detail.append(reading);
      if (state.active === "memories") {
        reading.append(text("h3", row.text.split(/\r?\n/)[0].slice(0, 45)));
        const record = el("div", "om-record");
        renderMemories(record, [row], load, resultState); reading.append(record);
        const recordActions = record.querySelector(".om-record-actions");
        if (recordActions) {
          const footer = el("div", "om-detail-footer"); footer.append(recordActions); detail.append(footer);
        }
        if (row.updated_at) reading.append(text("p", "最近更新：" + formatTime(row.updated_at), "om-count"));
        const provenance = el("div", "om-provenance");
        provenance.append(button("查看关联原文", async () => {
          saveScroll(); state.active = "originals"; state.originals.source_id = row.source_id;
          state.originals.query = ""; state.originals.days = 0; state.originals.page = 1; state.originals.selected = null;
          await load(); root.setAttribute("data-reading", "true");
        }));
        reading.append(provenance);
      } else {
        reading.append(text("h3", row.speaker === "user" ? "用户来信" : "林离回信"),
          text("p", row.created_at ? formatTime(row.created_at) : "时间未知", "om-count"), text("p", row.text, "om-original-text"));
        if (row.excerpt) reading.append(text("p", "当前为原文节选。", "om-count"), button("读取完整原文", async () => {
          const selected = key(row), collection = state.active;
          try {
            const payload = await requestJson(MEMORY_PATH, {browse: 1, collection: "originals", source_id: row.source_id, full: 1, limit: 20});
            if (collection !== state.active || state[collection].selected !== selected) return;
            const full = payload.originals?.find(item => item.source_id === row.source_id && item.speaker === row.speaker);
            if (full) selectRow(full); else resultState.textContent = "这段原文已不可用，可刷新列表。";
          } catch (_) { resultState.textContent = "完整原文读取失败，可稍后重试。"; }
        }));
        const footer = el("div", "om-detail-footer");
        footer.append(button("返回记忆", () => { saveScroll(); state.active = "memories"; load(); }));
        detail.append(footer);
      }
      root.setAttribute("data-reading", "true");
    };
    const syncTools = () => {
      const view = state[state.active]; input.value = view.query;
      input.placeholder = state.active === "memories" ? "搜索全部记忆" : "搜索全部信件原文";
      input.setAttribute("aria-label", input.placeholder); days.value = String(view.days); sort.value = view.sort;
      sourceFilter.hidden = !(state.active === "originals" && view.source_id);
      for (const tab of tabs.querySelectorAll("button")) tab.setAttribute("aria-selected", String(tab.dataset.collection === state.active));
    };
    for (const [collection, label] of [["memories","记忆"],["originals","信件原文"]]) {
      const tab = button(label, () => { saveScroll(); state.active = collection; root.setAttribute("data-reading", "false"); load(); });
      tab.dataset.collection = collection; tab.className = "om-tab"; tab.setAttribute("role", "tab"); tabs.append(tab);
    }
    const load = async () => {
      const id = ++generation, collection = state.active, view = state[collection];
      syncTools(); previous.disabled = true; next.disabled = true;
      resultState.textContent = "正在读取……"; setDiagnosticDetails(resultState, []);
      if (capability?.reason_code === "MEMORY_ADMIN_PAUSED") {
        list.replaceChildren(text("p", "长期记忆已暂停。恢复后可继续浏览。", "om-empty"));
        detail.replaceChildren(); pageLabel.textContent = ""; resultState.textContent = ""; return;
      }
      try {
        const payload = await requestJson(MEMORY_PATH, {browse: 1, collection, query: view.query,
          page: view.page, limit: 20, days: view.days, sort: view.sort, source_id: view.source_id});
        if (id !== generation || panel.isConnected === false) return;
        const loaded = (collection === "memories" ? payload.memories : payload.originals) || [];
        const total = Number.isInteger(payload.total) ? payload.total : loaded.length;
        const pages = Math.max(1, Math.ceil(total / 20)); view.page = payload.page || 1;
        count.textContent = (view.query ? "找到 " + total + " 条匹配结果" : "共 " + total + " 条" + (collection === "memories" ? "记忆" : "原文")) + " · 每页 20 条";
        pageLabel.textContent = "第 " + view.page + " / " + pages + " 页"; previous.disabled = view.page <= 1; next.disabled = view.page >= pages;
        list.replaceChildren();
        for (const row of loaded) {
          const item = button("", () => selectRow(row)); item.className = "om-row"; item.dataset.memoryKey = key(row);
          item.append(text("span", collection === "memories" ? row.text.split(/\r?\n/)[0] : row.speaker === "user" ? "用户来信" : "林离回信", "om-row-title"),
            text("span", row.text, "om-row-excerpt"), text("span", (row.updated_at || row.created_at) ? formatTime(row.updated_at || row.created_at) : "时间未知", "om-row-time"));
          list.append(item);
        }
        const selected = loaded.find(row => key(row) === view.selected) || loaded[0];
        if (selected) selectRow(selected);
        else {
          list.append(text("p", view.query ? "没有匹配结果，请尝试其他关键词或时间范围。" : "暂无记录，可更换时间范围查看。", "om-empty"));
          detail.replaceChildren(text("p", view.source_id ? "没有找到关联原文。这条记忆可能来自手动添加或更正。" : "选中一条记录，在这里阅读完整内容。", "om-empty"));
        }
        root.setAttribute("data-reading", "false"); list.scrollTop = view.scroll;
        if (collection === "memories" && Number.isInteger(payload.total_count)) updateSummary({...capability, count: payload.total_count});
        if (id === generation) resultState.textContent = "";
      } catch (error) {
        if (id !== generation || panel.isConnected === false) return;
        if (collection === "memories") updateSummary({state: "unavailable"});
        resultState.textContent = "读取失败，已显示的记录保留。请点击重试。";
        setDiagnosticDetails(resultState, error?.code || "COMPANION_READ_UNAVAILABLE");
      }
    };
    let searchTimer;
    input.addEventListener("input", () => {
      window.clearTimeout(searchTimer); generation++;
      state[state.active].query = input.value.trim(); state[state.active].page = 1; state[state.active].scroll = 0;
      if (state.active === "originals") state.originals.source_id = null;
      searchTimer = window.setTimeout(load, 250);
    });
    input.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); window.clearTimeout(searchTimer); load(); } });
    for (const [control, name] of [[days,"days"],[sort,"sort"]]) control.addEventListener("change", () => {
      state[state.active][name] = name === "days" ? Number(control.value) : control.value; state[state.active].page = 1; state[state.active].scroll = 0; load();
    });
    root.addEventListener("keydown", event => {
      if (event.key === "Escape" && !management.hidden) { event.stopPropagation(); closeManagement.click(); }
      else if (event.key === "Escape" && root.getAttribute("data-reading") === "true" && window.matchMedia?.("(max-width:700px)").matches) {
        event.stopPropagation(); root.setAttribute("data-reading", "false");
      }
    });
    pagination.append(previous, pageLabel, next); listPanel.append(count, list, pagination); workspace.append(listPanel, detail);
    toolbar.append(tabs, tools, sourceFilter); shell.append(toolbar, workspace); resultState.className = "om-result";
    tools.append(button("刷新 / 重试", () => { saveScroll(); load(); }));
    root.append(style, heading, shell, resultState, management);
    return {root, load, managementControls};
  };

  const renderMemoryPanel = async (panel, capability) => {
    window.clearTimeout(panel.__oliviaMemoryStatusTimer);
    const state = capabilityState(capability);
    const confirmClear = async () => await confirmAction("确认清空当前用户的 Mem0 长期记忆？")
      && await confirmAction("清空后无法恢复。原始信件和林离世界不会受影响，仍要继续吗？");
    if (state === "disabled" || state === "unavailable") {
      if (state === "unavailable" && capability && capability.reason_code === "MEM0_INITIALIZING") {
        panel.replaceChildren(
          text("h3", "长期记忆", "text-text-title text-title-m"),
          text("p", "长期记忆正在准备，其他功能可正常使用。需要长期记忆的回信会在准备完成后继续。", "text-text-secondary text-body-m font-regular")
        );
        scheduleMemoryStatusRefresh(panel);
        return;
      }
      if (state === "unavailable" && capability && capability.reason_code === "MEMORY_ADMIN_CLEAR_PENDING") {
        const heading = text("h3", "长期记忆", "text-text-title text-title-m");
        const summary = text("p", "上次清空尚未完成。", "text-text-secondary text-body-m font-regular");
        const resultState = text("p", "", "text-text-secondary text-body-m font-regular");
        const resume = button("继续完成清空", async () => {
          if (!await confirmClear()) return;
          setButtonsBusy([resume], true);
          resultState.textContent = "正在继续清空当前用户记忆……";
          setDiagnosticDetails(resultState, []);
          try {
            const payload = await requestMutation(MEMORY_CLEAR_PATH, {
              request_id: requestId("memory.clear"),
              reason: "用户在原版 Olivia 设置中确认继续清空当前长期记忆。",
              confirmed: true,
            });
            resultState.textContent = mutationMessage(payload, "当前用户长期记忆已清空。");
            const status = await requestJson(STATUS_PATH);
            await renderMemoryPanel(panel, status.capabilities.memory);
          } catch (_error) {
            resultState.textContent = memoryClearFailureMessage(_error);
            setDiagnosticDetails(resultState, _error?.code);
          } finally {
            setButtonsBusy([resume], false);
          }
        });
        panel.replaceChildren(heading, summary, resume, resultState);
        return;
      }
      if (state === "unavailable" && capability && capability.reason_code === "MEM0_EMBEDDING_CACHE_UNAVAILABLE") {
        const heading = text("h3", "长期记忆", "text-text-title text-title-m");
        const embedding = capability.embedding && typeof capability.embedding === "object"
          ? capability.embedding
          : { state: "missing" };
        const installState = typeof embedding.state === "string" ? embedding.state : "error";
        const summary = text(
          "p",
          installState === "ready"
            ? "Embedding 已就绪。重启本机服务后，长期记忆会离线运行。"
            : installState === "installing"
            ? "正在安装 Embedding，请保持此页面打开。"
            : installState === "error"
            ? "Embedding 安装失败，请重试。"
            : "Embedding 尚未安装，长期记忆暂不可用。",
          "text-text-secondary text-body-m font-regular"
        );
        const resultState = text(
          "p",
          "",
          "text-text-secondary text-body-m font-regular"
        );
        resultState.setAttribute("aria-live", "polite");
        const refresh = async () => {
          try {
            const payload = await requestJson(STATUS_PATH);
            const capabilities = payload.capabilities && typeof payload.capabilities === "object"
              ? payload.capabilities
              : {};
            const latest = capabilities.memory;
            await renderMemoryPanel(panel, latest);
          } catch (_error) {
            resultState.textContent = "安装仍在进行，可稍后刷新。";
            window.setTimeout(refresh, 1000);
          }
        };
        if (installState === "installing") {
          panel.replaceChildren(heading, summary, resultState);
          window.setTimeout(refresh, 1000);
          return;
        }
        if (installState === "ready") {
          panel.replaceChildren(heading, summary, resultState);
          return;
        }
        const install = button("导入记忆离线包", async () => {
          setButtonsBusy([install], true);
          resultState.textContent = "正在安装 Embedding……";
          try {
            const payload = await requestCapability(MEM0_CAPABILITY_ACTION_PATH, {
              action: "import_offline",
            });
            if (payload.status === "CANCELLED") {
              resultState.textContent = "已取消导入。";
            } else if (["APPLIED", "NOOP"].includes(payload.status) || ["queued", "downloading", "verifying", "ready"].includes(payload.state)) {
              resultState.textContent = "已提交离线导入，可在“本地组件”查看进度。";
            } else {
              resultState.textContent = "Embedding 安装失败，请重试。";
            }
          } catch (_error) {
            resultState.textContent = "Embedding 安装失败，请重试。";
          } finally {
            setButtonsBusy([install], false);
          }
        });
        panel.replaceChildren(heading, summary, install, resultState);
        return;
      }
      if (state === "unavailable") {
        const resultState = text("p", "", "text-text-secondary text-body-m font-regular");
        const retry = button("重新准备长期记忆", async () => {
          const exhausted = capability && capability.reason_code === "MEMORY_OUTBOX_RETRY_EXHAUSTED";
          if (exhausted && !await confirmAction("为重试耗尽的记忆任务各重试一次？这会调用已配置的大模型并消耗额度，已有记忆会保留。")) return;
          setButtonsBusy([retry], true);
          try {
            const payload = await requestMutation(MEMORY_RETRY_PATH, exhausted ? {retry_failed_writes: true} : {});
            if (payload.retried_count > 0) {
              resultState.textContent = `已安排 ${payload.retried_count} 项记忆任务重试，请稍后查看。`;
              return;
            }
            if (["INITIALIZING", "AVAILABLE"].includes(payload.status)) {
              const status = await requestJson(STATUS_PATH);
              await renderMemoryPanel(panel, status.capabilities.memory);
              return;
            }
            resultState.textContent = "当前配置仍未就绪，请检查长期记忆下载状态。";
          } catch (_error) {
            resultState.textContent = "长期记忆仍未准备好，请稍后重试。";
          } finally {
            setButtonsBusy([retry], false);
          }
        });
        panel.replaceChildren(
          text("h3", "长期记忆", "text-text-title text-title-m"),
          text("p", "长期记忆没有准备成功，其他功能仍可使用；等待中的回信会保留。", "text-text-secondary text-body-m font-regular"),
          retry,
          resultState
        );
        return;
      }
      renderUnavailable(panel, state, "长期记忆");
      return;
    }


    const paused = capability && capability.reason_code === "MEMORY_ADMIN_PAUSED";
    const summary = text("p", "", "text-text-secondary text-body-m font-regular");
    const updateSummary = (latest) => {
      const count = latest && latest.count;
      const isPaused = latest && latest.reason_code === "MEMORY_ADMIN_PAUSED";
      summary.textContent = "状态：" + (isPaused ? "已暂停" : stateLabels[capabilityState(latest)]) +
        (Number.isInteger(count) ? " · " + count + " 条记忆" : "");
    };
    updateSummary(capability);
    const resultState = text("p", "", "text-text-secondary text-body-m font-regular");
    resultState.setAttribute("aria-live", "polite");
    const browser = createMemoryBrowser(panel, capability, summary, resultState, updateSummary);
    const load = browser.load;

    const lifecycleControls = actions();
    const refreshLifecyclePanel = async () => {
      const payload = await requestJson(STATUS_PATH);
      const capabilities = payload.capabilities && typeof payload.capabilities === "object"
        ? payload.capabilities
        : {};
      await renderMemoryPanel(panel, capabilities.memory);
    };
    const toggle = button(paused ? "恢复长期记忆" : "暂停长期记忆", async () => {
      const action = paused ? "恢复" : "暂停";
      if (!await confirmAction(`确认${action} Mem0 长期记忆？Archive 和林离世界不会受影响。`)) {
        return;
      }
      setButtonsBusy([toggle, clear], true);
      resultState.textContent = `正在${action}长期记忆……`;
      try {
        const payload = await requestMutation(
          paused ? MEMORY_RESUME_PATH : MEMORY_PAUSE_PATH,
          {
            request_id: requestId(paused ? "memory.resume" : "memory.pause"),
            reason: `用户在原版 Olivia 设置中明确${action} Mem0 长期记忆。`,
          }
        );
        resultState.textContent = mutationMessage(payload, `长期记忆已${action}。`);
        await refreshLifecyclePanel();
      } catch (_error) {
        resultState.textContent = `长期记忆${action}失败。`;
      } finally {
        setButtonsBusy([toggle, clear], false);
      }
    });
    const clear = button("清空当前用户记忆", async () => {
      if (!await confirmClear()) return;
      setButtonsBusy([toggle, clear], true);
      resultState.textContent = "正在清空当前用户记忆……";
      setDiagnosticDetails(resultState, []);
      try {
        const payload = await requestMutation(MEMORY_CLEAR_PATH, {
          request_id: requestId("memory.clear"),
          reason: "用户在原版 Olivia 设置中明确清空当前长期记忆。",
          confirmed: true,
        });
        resultState.textContent = mutationMessage(payload, "当前用户长期记忆已清空。"
        );
        await refreshLifecyclePanel();
      } catch (_error) {
        resultState.textContent = memoryClearFailureMessage(_error);
        setDiagnosticDetails(resultState, _error?.code);
      } finally {
        setButtonsBusy([toggle, clear], false);
      }
    });
    const retryWrites = button("重试未写入的记忆", async () => {
      if (!await confirmAction("为重试耗尽的记忆任务各重试一次？这会调用已配置的大模型并消耗额度；已有信件和记忆会保留。")) return;
      setButtonsBusy([retryWrites], true);
      try {
        const payload = await requestMutation(MEMORY_RETRY_PATH, {retry_failed_writes: true});
        resultState.textContent = payload.retried_count > 0
          ? `已安排 ${payload.retried_count} 项记忆任务重试，请稍后查看。`
          : "没有可重试的任务；若长期记忆已暂停，请先恢复。";
      } catch (_error) {
        resultState.textContent = "未能安排重试，请导出诊断包。";
      } finally {
        setButtonsBusy([retryWrites], false);
      }
    });
    lifecycleControls.append(toggle, clear, retryWrites);
    browser.managementControls.append(lifecycleControls);
    panel.replaceChildren(browser.root);
    await load();
  };

  const renderPrivateWorldPanel = async (panel, privateCapability) => {
    if (!panel) return;
    const rawPrivateWorldState = privateCapability
      && typeof privateCapability.state === "string"
      ? privateCapability.state
      : null;
    const privateState = privateWorldState(privateCapability);
    const heading = text("h3", "林离的生活", "text-text-title text-title-m");
    const summary = text(
      "p",
      `状态：${stateLabels[privateState]}`,
      "text-text-secondary text-body-m font-regular"
    );
    const reasonCode = rawPrivateWorldState === "unavailable"
      && privateCapability
      && typeof privateCapability.reason_code === "string"
      && /^[A-Z][A-Z0-9_]{0,95}$/.test(privateCapability.reason_code)
      ? privateCapability.reason_code
      : null;
    if (privateState !== "available") {
      panel.replaceChildren(
        heading,
        summary,
        text(
          "p",
          "生活记录暂时无法读取，请稍后重新打开此页面。",
          "text-text-secondary text-body-m font-regular"
        )
      );
      setDiagnosticDetails(summary, reasonCode);
      return;
    }
    const requestToken = {};
    panel._lifeRequest = requestToken;
    const alive = () => panel.isConnected !== false && panel._lifeRequest === requestToken;
    const labels = { planned: "打算做", ongoing: "进行中", paused: "暂时搁下", completed: "已完成", cancelled: "已取消", awaiting_user: "等你说说后续" };
    const when = (value) => {
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
    };
    const card = () => {
      const el = document.createElement("article");
      el.style.cssText = "padding:16px;border-radius:12px;background:rgba(255,255,255,.045);display:grid;gap:10px;min-width:0;overflow-wrap:anywhere";
      return el;
    };
    const section = (title, subtitle) => {
      const el = document.createElement("section");
      el.style.cssText = "display:grid;gap:12px;margin-top:20px";
      el.append(text("h4", title, "text-text-title text-title-s"));
      if (subtitle) el.append(text("p", subtitle, "text-text-secondary text-caption-m"));
      return el;
    };
    const topic = (el, item) => {
      const draft = document.createElement("textarea");
      draft.readOnly = true;
      draft.hidden = true;
      draft.setAttribute("aria-label", "写信开头，可复制到信箱");
      draft.style.cssText = "width:100%;min-height:80px;background:transparent;color:inherit;padding:10px;border:1px solid #555;border-radius:8px";
      draft.value = `林离，我看到你${when(item.updated_at || item.occurred_at)}留下的近况：「${item.detail || item.note}」想跟你聊聊这件事。`;
      const hint = text("p", "复制到信箱，再写下你想说的话；不会自动寄出。", "text-text-secondary text-caption-m");
      hint.hidden = true;
      el.append(button("围绕这件事写信", () => {
        draft.hidden = false;
        hint.hidden = false;
        draft.focus();
        draft.select();
      }), draft, hint);
    };
    const status = text("p", "读取近况…", "text-text-secondary text-body-m");
    let busy = false;
    let attempted = false;
    const momentRow = (moment) => {
      const content = moment.content;
      const body = moment.kind === "media" ? `${content.delivery?.summary || "已送达回信"} · ${content.delivery?.presentation === "video" ? "视频" : "音频"}` : moment.kind === "daily" ? content.note : content.current ? content.current.note : (content.updates || []).map(item => item.detail).join(" · ");
      const el = document.createElement("details");
      el.style.cssText = "padding:10px 0;overflow-wrap:anywhere";
      el.append(text("summary", `${when(moment.occurred_at)} · ${body.slice(0, 54)}${body.length > 54 ? "…" : ""}`, "text-text-body text-body-m"));
      el.append(text("p", body, "text-text-body text-body-m"));
      for (const item of content.progress || content.updates || []) el.append(text("p", `${item.title}：${item.detail}`, "text-text-secondary text-body-m"));
      topic(el, {note: body, occurred_at: moment.occurred_at});
      return el;
    };
    const historyPanel = () => {
      const archive = document.createElement("details");
      archive.style.cssText = "margin-top:20px;overflow-wrap:anywhere";
      archive.append(text("summary", "翻看以前的生活片段", "text-text-title text-label-l"));
      const body = document.createElement("div");
      const feedback = text("p", "", "text-text-secondary text-caption-m");
      feedback.setAttribute("role", "status");
      archive.append(feedback, body);
      let pending = false, loaded = false, page = 0;
      const cursors = [null];
      const show = async (index) => {
        if (pending || !alive() || !archive.isConnected) return;
        pending = true;
        feedback.textContent = "读取历史片段…";
        try {
          const before = cursors[index];
          const result = await requestJson(DAILY_LIFE_PATH, {history: 1, before});
          if (!alive() || !archive.isConnected) return;
          if (result.schema_version !== "olivia.daily-life.history.v1" || !Array.isArray(result.moments)) throw new Error("DAILY_LIFE_INVALID");
          const navigation = document.createElement("div");
          navigation.style.cssText = "display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:12px";
          const previous = button("上一页", () => show(page - 1));
          const next = button("下一页", () => show(page + 1));
          previous.disabled = index === 0;
          next.disabled = !result.next_cursor;
          page = index;
          cursors[index + 1] = result.next_cursor;
          navigation.append(previous, text("span", `第 ${index + 1} 页`, "text-text-secondary text-caption-m"), next);
          body.replaceChildren(...result.moments.map(momentRow), navigation);
          feedback.textContent = result.moments.length ? "每页最多 8 条，按时间从新到旧。" : "还没有历史片段。";
          loaded = true;
        } catch (_error) {
          feedback.replaceChildren(text("span", "历史暂时没能读取，已显示的内容仍然保留。 ", "text-text-secondary text-caption-m"), button("重试读取历史", () => show(index)));
        } finally { pending = false; }
      };
      archive.addEventListener("toggle", () => { if (archive.open && !loaded) show(0); });
      return archive;
    };
    const relationshipPanel = () => {
      const area = document.createElement("details");
      area.style.cssText = "margin-top:20px;overflow-wrap:anywhere";
      area.append(text("summary", "你们的关系", "text-text-title text-label-l"));
      const content = document.createElement("div");
      content.style.cssText = "display:grid;gap:10px;padding-top:12px";
      area.append(content);
      let pending = false, loaded = false;
      const read = async () => {
        if (pending || !alive() || !area.isConnected) return;
        pending = true;
        if (!loaded) content.replaceChildren(text("p", "读取关系状态…", "text-text-secondary text-body-m"));
        try {
          const result = await requestJson(PRIVATE_WORLD_PATH);
          if (!alive() || !area.isConnected) return;
          if (!result || result.status !== "READY" || !result.levels) throw new Error("RELATIONSHIP_UNAVAILABLE");
          const stages = {unknown:"尚未确定", acquaintance:"初识", familiar:"逐渐熟悉", friend:"朋友", trusted_friend:"信赖的朋友", close:"亲近", committed:"稳定的亲密关系"};
          const levels = {unknown:"尚未确定", low:"较低", medium:"中等", high:"较高"};
          content.replaceChildren(text("p", `关系阶段：${stages[result.relationship_stage] || "尚未确定"}`, "text-text-body text-body-m"));
          const list = document.createElement("dl");
          list.style.cssText = "display:grid;grid-template-columns:auto 1fr;gap:8px 24px;margin:0";
          for (const [key, label] of [["familiarity","熟悉"],["trust","信任"],["comfort","自在"],["closeness","亲近"],["tension","紧张"]]) {
            const value = text("dd", levels[result.levels[key]] || "尚未确定", "text-text-body text-body-m");
            value.style.margin = "0";
            list.append(text("dt", label, "text-text-secondary text-body-m"), value);
          }
          content.append(list, text("p", "这里读取既有关系记录；生活动态的刷新不会增加好感或改变关系阶段。", "text-text-secondary text-caption-m"));
          loaded = true;
        } catch (_error) {
          content.replaceChildren(text("p", "关系状态暂时无法读取。", "text-text-secondary text-body-m"), button("重试读取关系", read));
        } finally { pending = false; }
      };
      area.addEventListener("toggle", () => { if (area.open && !loaded) read(); });
      area.refresh = read;
      return area;
    };
    const relationship = relationshipPanel();
    const agendaRows = (world) => {
      const date=world?.schedule?.date;
      const meals=date && Array.isArray(world.meals) ? world.meals.filter(item=>item.date===date) : [];
      const plans=date && Array.isArray(world?.meal_schedule) ? world.meal_schedule.filter(item=>item.date===date) : [];
      const stamp=value=>typeof value==='string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? Date.parse(value) : NaN;
      const clock=value=>Number.isFinite(stamp(value)) ? new Date(value).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}) : '';
      const span=(start,end)=>{const a=clock(start),b=clock(end);return a ? a+(b && b!==a ? '–'+b : '') : '时间待同步';};
      const rows=(world?.schedule?.classes || []).map(item=>({kind:'book',at:stamp(item.start),label:`${span(item.start,item.end)}　${item.title} · 课表计划`}));
      const seenActivities=new Set();
      for(const item of world?.today_activities || []){
        const at=stamp(item.occurred_at);
        if(!Number.isFinite(at) || typeof item.activity!=='string' || item.activity_kind==='meal')continue;
        const day=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(at));
        if(day!==date || seenActivities.has(item.source_id))continue;
        seenActivities.add(item.source_id);
        rows.push({kind:'clock',at,label:`${span(item.occurred_at,item.last_recorded_at)}　${item.activity} · ${item.record_count > 1 ? '同状态记录' : '生活记录'}`,note:typeof item.note==='string'?item.note:''});
      }
      const names={breakfast:'早餐',lunch:'午餐',dinner:'晚餐',snack:'加餐'};
      const slots=['breakfast','lunch','dinner'];
      if(meals.some(item=>item.slot==='snack'))slots.push('snack');
      for(const slot of slots){
        const item=meals.find(entry=>entry.slot===slot);
        const plan=plans.find(entry=>entry.slot===slot);
        let at=NaN,label;
        const failure=plan?.status==='error' ? `用餐更新失败${/^[A-Z][A-Z0-9_]{0,95}$/.test(plan.error_code || '') ? `（${plan.error_code}）` : ''}` : null;
        if(!date)label=`${names[slot]} · 当天日期待同步`;
        else if(!item){
          at=stamp(plan?.scheduled_for);
          const state=failure || (plan?.status==='not_due' ? '未到用餐时间' : plan?.status==='pending' ? '用餐记录待更新' : '用餐安排待同步');
          label=`${Number.isFinite(at) ? '计划 '+clock(plan.scheduled_for)+'　' : ''}${names[slot]} · ${state}`;
        }else{
          const planned=item.status==='planned';
          const start=planned ? (item.scheduled_for || plan?.scheduled_for) : (item.started_at || item.occurred_at);
          at=stamp(start);
          const food=typeof item.food==='string' ? item.food : '';
          const states={planned:`计划吃${food}`,eating:`正在吃${food}`,eaten:`已吃${food}`,skipped:'这餐没吃'};
          const state=states[item.status] || '用餐记录待更新';
          label=`${planned ? '计划 ' : ''}${span(start,item.status==='eaten' ? item.finished_at : null)}　${names[slot]} · ${item.stale ? `上次记录：${state}；当前状态待更新` : state}`;
          if(item.stale && failure)label+=` · ${failure}`;
          if(item.recovered)label+=` · ${clock(item.recorded_at) ? '补记于 '+clock(item.recorded_at) : '补记'}`;
        }
        rows.push({kind:'meal',at,label});
      }
      return rows.sort((a,b)=>(Number.isFinite(a.at)?a.at:Infinity)-(Number.isFinite(b.at)?b.at:Infinity));
    };
    const draw = (payload) => {
      if (!payload || payload.schema_version !== "olivia.daily-life.v1" || !Array.isArray(payload.projects)
          || !Array.isArray(payload.shared) || !Array.isArray(payload.moments)) throw new Error("DAILY_LIFE_INVALID");
      const now = section("此刻的林离", payload.stale ? "这是她最近留下的近况，不代表此刻仍在做同一件事。" : "她愿意与你分享的一小段生活。");
      if (payload.rhythm) {
        now.append(text("p", payload.rhythm.activity, "text-text-title text-title-s"),
          text("p", payload.rhythm.note, "text-text-secondary text-body-m"));
        if (payload.rhythm.wellbeing && payload.rhythm.wellbeing.state !== "well") {
          now.append(text("p", payload.rhythm.wellbeing.summary, "text-text-secondary text-body-m"));
        }
      }
      if (payload.current) {
        const current = payload.current;
        const el = card();
        if (payload.rhythm) el.append(text("small", "最近一次分享（不是实时活动）", "text-text-secondary text-caption-m"));
        el.append(text("p", [current.location, current.activity].filter(Boolean).join(" · ") || "她的近况", "text-text-title text-title-s"),
          text("p", current.note, "text-text-body text-body-m"),
          text("small", when(current.occurred_at), "text-text-secondary text-caption-m"));
        topic(el, current);
        now.append(el);
      } else now.append(text("p", "还没有留下近况。连接大模型后，这里会开始记录她的生活。", "text-text-secondary text-body-m"));
      if (payload.world) {
        const world = payload.world;
        const schedule = world.schedule || {};
        {
          const el = card();
          const phases = {teaching:'教学期间', assessment:'复习与考核期间', vacation:'假期', holiday:'休息日', graduated:'毕业后的生活', before_enrollment:'入学前'};
          el.append(text('h5', '今天的安排', 'text-text-title text-label-l'),
            text('small', `${schedule.date || '当天日期待同步'} · ${phases[schedule.phase] || ''}`, 'text-text-secondary text-caption-m'));
          for (const item of agendaRows(world)) {
            el.append(text('p', item.label, 'text-text-body text-body-m'));
            if(item.note)el.append(text('small',item.note,'text-text-secondary text-caption-m'));
          }
          el.append(text('small', '计划不等于已经发生，临时变化以她留下的近况为准。', 'text-text-secondary text-caption-m'));
          now.append(el);
        }
        const weather = world.weather;
        if (weather) {
          const el = card();
          el.append(text('h5', '上海天气', 'text-text-title text-label-l'));
          el.append(text('p', weather.status === 'fresh' ? `虹桥站观测 ${weather.temperature_c} °C` : '当前天气暂未获取', 'text-text-body text-body-m'));
          if (weather.observed_at) el.append(text('small', `${weather.status === 'stale' ? '上次观测（已过期）' : '观测时间'}：${when(weather.observed_at)} · 虹桥站，不代表全市每一处`, 'text-text-secondary text-caption-m'));
          now.append(el);
        }
      }
      const feelings = card();
      feelings.append(text('h5', '她现在的心情', 'text-text-title text-label-l'));
      const emotion = payload.emotion;
      const reactions = {pleased:'开心', anticipation:'期待', relieved:'松了口气', moved:'感动', affection:'心动', shy:'害羞',
        missing:'想你', angry:'生气', frustrated:'烦躁', jealous:'吃醋', sad:'难过', disappointed:'失落', hurt:'委屈', lonely:'孤单',
        concerned:'担心', afraid:'不安', surprised:'惊讶', bored:'无聊', calm:'平静'};
      const graded=(label,intensity)=>{
        const name=reactions[label];
        if(!name || ['calm','relieved'].includes(label))return name;
        return intensity==='low' ? `有点${name}` : intensity==='high' ? `很${name}` : name;
      };
      const hasCurrentAffect=Boolean(emotion && Object.prototype.hasOwnProperty.call(emotion,'current_affect'));
      const affect=emotion?.current_affect, affectName=graded(affect?.label, affect?.intensity);
      const affectState=affect?.status==='available' && affectName ? affectName
        : ['stale','missing'].includes(affect?.status) ? '平静' : '暂时读不到';
      const affectReason=()=>{
        const kind=String(affect?.basis?.kind || '');
        const group=kind==='body' ? '身体和作息的状态'
          : kind==='progress' || kind.startsWith('projects_') ? '在意的事情有了进展或结果'
          : kind==='interaction' || kind.startsWith('reactions_') ? '和你刚才的交流'
          : kind==='concern' || kind.startsWith('concerns_') ? '还有放不下的事'
          : kind==='life' || kind.startsWith('published_moments_') || kind.startsWith('episode_') ? '最近生活里的经历' : '';
        const ids=new Set(affect?.basis?.source_ids || []);
        const related=(emotion?.reactions || []).find(item=>ids.has(item.source_id) && typeof item.goal_or_need==='string' && item.goal_or_need.trim());
        if(!group)return '';
        return `因为${group}${related ? `，她在意的是${related.goal_or_need.trim()}` : ''}`;
      };
      const appendAffectBasis=target=>{
        if(!affectName || affect?.status!=='available')return;
        const reason=affectReason();
        if(reason)target.append(text('p',reason,'text-text-secondary text-body-m'));
      };
      if(hasCurrentAffect){feelings.append(text('p',`心情：${affectState}`,'text-text-title text-label-l'));appendAffectBasis(feelings);}
      if (!emotion || emotion.status !== 'available') {
        feelings.append(text('p', hasCurrentAffect ? '情绪变化记录暂时无法读取。' : '当前情绪暂时无法读取，不能据此判断她心情平静。', 'text-text-secondary text-body-m'));
      } else {
        const recent = (emotion.reactions || []).filter(item => reactions[item.reaction]);
        if (!recent.length) feelings.append(text('p', '暂时没有有效的近期情绪记录，不代表没有情绪。', 'text-text-secondary text-body-m'));
        for (const item of [...recent].reverse()) {
          const entry = document.createElement('div');
          entry.style.cssText = 'display:grid;gap:6px;padding:8px 0';
          entry.append(text('p', reactions[item.reaction], 'text-text-title text-label-l'),
            text('p', `相关事件或话语：${item.quote}`, 'text-text-body text-body-m'),
            text('p', `她在意的是：${typeof item.goal_or_need === 'string' && item.goal_or_need.trim() ? item.goal_or_need : '尚未明确'}`, 'text-text-secondary text-body-m'),
            text('small', when(item.occurred_at), 'text-text-secondary text-caption-m'));
          feelings.append(entry);
        }
        if ((emotion.concerns || []).length) {
          feelings.append(text('h5', '挂心的事', 'text-text-title text-label-l'));
          for (const item of emotion.concerns) feelings.append(text('p', item.summary, 'text-text-body text-body-m'));
        }
        feelings.append(text('small', '这些是她对事件的暂时理解；多种感受可能并存，旧反应会随时间淡出。', 'text-text-secondary text-caption-m'));
      }
      now.prepend(feelings);
      const projects = section("最近在忙", "有些事会慢慢来，也可以暂时搁下。");
      const shared = section("与你有关", "推荐、约定，以及你留下的参与。");
      for (const [target, items, empty, limit] of [[projects, payload.projects, "她还没有提起正在忙的事。", 3], [shared, payload.shared, "你们的共同事项会从信件中慢慢留下来。", 2]]) {
        const more = document.createElement("details");
        more.append(text("summary", "其他事项与已结束的约定", "text-text-secondary text-caption-m"));
        let shown = 0;
        for (const item of items) {
          const el = document.createElement("details");
          el.style.cssText = "padding:8px 0;overflow-wrap:anywhere";
          el.append(text("summary", `${item.title} · ${item.time_scope === 'transient' ? '当时的活动记录' : item.deadline_expired ? (item.time_scope_pending ? '时限待确认（上次时限已过）' : '时限已过，进展未确认') : (labels[item.status] || "进展未知") + (item.time_scope_pending ? ' · 时限待确认' : '')}`, "text-text-title text-label-l"),
            text("p", item.detail, "text-text-body text-body-m"),
            text("small", when(item.updated_at), "text-text-secondary text-caption-m"));
          if(item.deadline_at)el.append(text('small',`${item.time_scope_pending?'上次时限':'时限'}：${when(item.deadline_at)}`,'text-text-secondary text-caption-m'));
          topic(el, item);
          if (item.time_scope !== 'transient' && !item.deadline_expired && !["completed", "cancelled"].includes(item.status) && shown < limit) { target.append(el); shown++; }
          else more.append(el);
        }
        if (more.children.length > 1) target.append(more);
        if (!items.length) target.append(text("p", empty, "text-text-secondary text-body-m"));
      }
      const episodeHistory=document.createDocumentFragment();
      const episodes=Array.isArray(payload.world?.recent_episodes) ? payload.world.recent_episodes : [];
      if(episodes.length){
        const area=section('经历的过程','展开查看经过、她的理解，以及尚未发生的下一步打算。');
        const kinds={practice:'练琴',meal:'用餐',rest:'休息'};
        const results={completed:'完成',partial:'部分进展',failed:'未达成',paused:'暂停'};
        for(const episode of episodes){
          const entry=document.createElement('details');entry.className='olivia-world-episode';
          entry.style.cssText='padding:10px 0;overflow-wrap:anywhere';
          const result=results[episode.result?.status] || '结果待同步';
          entry.append(text('summary',`${when(episode.occurred_at)} · ${kinds[episode.activity_kind] || '生活经历'} · ${result}`,'text-text-body text-body-m'));
          if(episode.trigger?.detail)entry.append(text('p',`起因：${episode.trigger.detail}`,'text-text-body text-body-m'));
          if(Array.isArray(episode.process) && episode.process.length){
            entry.append(text('h5','已发生的过程','text-text-title text-label-l'));
            for(const step of episode.process){
              for(const [field,label] of [['obstacle','遇到的情况'],['response','她的应对'],['outcome','随后发生']]){
                if(step[field])entry.append(text('p',`${label}：${step[field]}`,'text-text-body text-body-m'));
              }
            }
          }
          if(episode.result?.detail)entry.append(text('p',`结果 · ${result}：${episode.result.detail}`,'text-text-body text-body-m'));
          if(episode.interpretation?.subjective===true && episode.interpretation.meaning)entry.append(text('p',`她的理解（主观）：${episode.interpretation.meaning}`,'text-text-secondary text-body-m'));
          if(episode.effects?.next_action)entry.append(text('p',`下一步打算（尚未发生）：${episode.effects.next_action}`,'text-text-secondary text-body-m'));
          if(episode.effects?.open_loop)entry.append(text('p',`仍待处理：${episode.effects.open_loop}`,'text-text-secondary text-body-m'));
          area.append(entry);
        }
        episodeHistory.append(area);
      }
      const moments = section("生活片段", "最近 3 条，展开可读全文。");
      const recent = payload.moments.filter(item => !payload.current || item.id !== payload.current.source_id).slice(0, 3);
      for (const moment of recent) moments.append(momentRow(moment));
      if (!recent.length) moments.append(text("p", "新的生活片段会慢慢留下来。", "text-text-secondary text-body-m"));
      status.textContent = payload.refreshing ? "正在整理新的近况，已有内容仍可阅读。"
        : payload.error_code ? "新近况暂时没能整理好，已有记录已保留。可以稍后重试。" : "近况已保存。";
      if (panel.dataset?.worldMain !== undefined) {
        // Static, authored line icons; all user/model content uses textContent.
        const paths = {
          home:'m3 10 9-7 9 7v11h-6v-8H9v8H3Z',
          clock:'M12 3a9 9 0 1 0 0 18 9 9 0 1 0 0-18M12 6v6l4 2',
          weather:'M9 4h6v3H9ZM10 7v8a4 4 0 1 0 4 0V7M12 11v7',
          emotion:'M12 3a9 9 0 1 0 0 18 9 9 0 1 0 0-18M8 9h.01M16 9h.01M8 15h8',
          thought:'M7 17a6 6 0 0 1-3-10 6 6 0 0 1 9-3 6 6 0 0 1 8 6 6 6 0 0 1-8 6ZM5 20a1 1 0 1 0 0 2 1 1 0 1 0 0-2',
          book:'M12 5c-3-3-7-2-9-1v16c3-2 6-2 9 0 3-2 6-2 9 0V4c-3-1-6-2-9 1Zm0 0v15',
          meal:'M4 3v5a3 3 0 0 0 6 0V3M7 3v18M18 13v8M18 3a3 5 0 1 0 0 10 3 5 0 1 0 0-10',
          calendar:'M5 5h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2M7 3v4M17 3v4M3 11h18',
          link:'m10 8 3-3a5 5 0 0 1 7 7l-3 3M14 16l-3 3a5 5 0 0 1-7-7l3-3M8 16l8-8'
        };
        const line = (kind, label, cls='olivia-world-line') => {
          const row=document.createElement('div');row.className=cls;
          const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
          svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');
          const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d',paths[kind] || paths.clock);svg.append(path);
          row.append(svg,text('span',label));return row;
        };
        const world=payload.world || {}, schedule=world.schedule || {}, weather=world.weather;
        const top=document.createElement('div');top.className='olivia-world-heading';
        const weatherLabel=weather?.status==='fresh' ? `上海 · 虹桥站 ${weather.temperature_c} °C` : '上海 · 天气暂未更新';
        const weatherInfo=line('weather',weatherLabel+' · 北京时间');
        if(weather?.observed_at)weatherInfo.title=`${weather.status==='stale'?'已过期的观测':'观测时间'}：${when(weather.observed_at)}`;
        const refresh=button('更新近况',()=>load(true));refresh.disabled=Boolean(payload.refreshing);top.append(weatherInfo,refresh);
        const overview=document.createElement('section');overview.className='olivia-world-overview';
        const current=payload.current, fresh=current && !payload.stale;
        const phase=payload.rhythm?.phase, restWindow=payload.rhythm?.planned_rest_window;
        const clock=value=>{const date=new Date(value);return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false,timeZone:'Asia/Shanghai'});};
        const heading=phase==='sleep' ? '已经睡下了'
          : phase==='interrupted_rest' ? '还醒着，在和你聊天'
          : phase==='bathing' ? '在洗澡'
          : fresh ? (current.activity || '她最近的近况')
          : current?.activity && current?.occurred_at ? `${when(current.occurred_at)} 在${current.activity}，之后还没有新动态`
          : (payload.rhythm?.activity || '暂时还没有她的近况');
        overview.append(text('h3',heading));
        const meta=document.createElement('div');meta.className='olivia-world-meta';
        const resting=['sleep','interrupted_rest','bathing'].includes(phase);
        if(resting && restWindow?.start && restWindow?.end && clock(restWindow.start) && clock(restWindow.end))
          meta.append(line('clock',`她计划 ${clock(restWindow.start)}–${clock(restWindow.end)} 休息`));
        else if(fresh && current.location)meta.append(line('home',current.location));
        if(!resting && fresh && current?.occurred_at)meta.append(line('clock',`记录于 ${when(current.occurred_at)}`));
        overview.append(meta);
        const mood=document.createElement('div');mood.className='olivia-world-mood';
        const valid=emotion?.status==='available';
        const recent=valid ? [...(emotion.reactions || [])].filter(item=>reactions[item.reaction]).reverse() : [];
        const emotionNames=[...new Set(recent.map(item=>reactions[item.reaction]))];
        mood.append(line('emotion',`心情：${hasCurrentAffect?affectState:!valid?'暂时读不到':emotionNames.length?emotionNames.join('、'):'平静'}`));
        appendAffectBasis(mood);
        const detail=document.createElement('details');detail.className='olivia-world-emotion-detail';
        detail.open=Boolean(panel._emotionOpen);
        detail.addEventListener('toggle',()=>{if(detail.isConnected)panel._emotionOpen=detail.open});
        detail.append(text('summary','查看变化与原因'));
        const changes=[];
        for(const item of recent){
          const key=JSON.stringify([item.reaction,item.quote,item.goal_or_need?.trim() || '',item.action_tendency || '']);
          const last=changes[changes.length-1];
          if(last?.key===key){last.count++;last.first=item.occurred_at;}
          else changes.push({key,item,count:1,first:item.occurred_at});
        }
        for(const {item,count,first} of changes){
          const entry=document.createElement('div');entry.className='olivia-world-emotion-entry';
          entry.append(text('h5',reactions[item.reaction]),text('p',`相关事件或话语：${item.quote}`),text('p',`她在意的需要：${typeof item.goal_or_need === 'string' && item.goal_or_need.trim() ? item.goal_or_need : '尚未明确'}`),text('small',count>1?`${when(first)} — ${when(item.occurred_at)} · 相同感受 ${count} 次`:when(item.occurred_at)));
          if(item.reaction==='relieved')entry.append(text('p','变化：对这件事感到释然，不等同于开心。'));
          detail.append(entry);
        }
        if(recent.length)mood.append(detail);
        if(valid && emotion.concerns?.length)mood.append(line('thought',`挂心的事：${emotion.concerns.map(item=>item.summary).join('；')}`));
        overview.append(mood);
        const tabs=document.createElement('div');tabs.className='olivia-world-tabs';tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','世界内容');
        const today=document.createElement('div');today.className='olivia-world-content';
        const columns=document.createElement('div');columns.className='olivia-world-columns';
        const agenda=section('今天的安排',schedule.date || '日期暂未获取');
        for(const item of agendaRows(world)){
          if(!item.note){agenda.append(line(item.kind,item.label,'olivia-world-agenda'));continue;}
          const entry=document.createElement('details');entry.className='olivia-world-agenda-detail';
          entry.append(text('summary',item.label,'olivia-world-agenda'),text('p',item.note,'olivia-world-muted'));
          agenda.append(entry);
        }
        agenda.append(text('small','课表和打算不证明已经发生，以后续生活记录为准。','olivia-world-muted'));
        const next=section('接下来','');next.className='olivia-world-aside';
        if(schedule.next_class)next.append(line('calendar',`${when(schedule.next_class.start)}　${schedule.next_class.title}`,'olivia-world-agenda'));
        else next.append(text('p','暂无下一节课的安排。','olivia-world-muted'));
        const promise=payload.shared.find(item=>item.time_scope !== 'transient' && !item.deadline_expired && !['completed','cancelled'].includes(item.status));
        if(promise){next.append(line('link','与你的约定','olivia-world-agenda'),text('p',promise.title),text('small',labels[promise.status] || '状态待确认','olivia-world-muted'))}
        columns.append(agenda,next);today.append(columns);
        const connections=document.createElement('div');connections.className='olivia-world-content';
        const relationColumns=document.createElement('div');relationColumns.className='olivia-world-columns';
        const related=document.createElement('div');related.className='olivia-world-aside';related.append(shared,relationship);relationColumns.append(projects,related);connections.append(relationColumns);
        const history=document.createElement('div');history.className='olivia-world-content';
        if(current){const last=section('最近一次分享',fresh?'最近留下的记录':'旧记录，不代表当前活动');last.append(text('p',current.note),text('small',when(current.occurred_at),'olivia-world-muted'));topic(last,current);history.append(last)}
        history.append(episodeHistory,moments,historyPanel());
        const views=[today,connections,history], names=['今天','牵挂与关系','生活记录'];
        const choose=index=>{
          panel._worldTab=index;
          [...tabs.children].forEach((tab,i)=>{tab.setAttribute('aria-selected',String(i===index));tab.tabIndex=i===index?0:-1;views[i].hidden=i!==index});
        };
        names.forEach((name,i)=>{
          const tab=button(name,()=>choose(i));tab.setAttribute('role','tab');tab.id=`olivia-world-tab-${i}`;tab.setAttribute('aria-controls',`olivia-world-view-${i}`);
          views[i].id=`olivia-world-view-${i}`;views[i].setAttribute('role','tabpanel');views[i].setAttribute('aria-labelledby',tab.id);
          tab.addEventListener('keydown',event=>{const index=event.key==='ArrowRight'?(i+1)%3:event.key==='ArrowLeft'?(i+2)%3:event.key==='Home'?0:event.key==='End'?2:null;if(index!==null){event.preventDefault();choose(index);tabs.children[index].focus()}});tabs.append(tab);
        });
        choose([0,1,2].includes(panel._worldTab)?panel._worldTab:0);
        const basis=document.createElement('details');basis.className='olivia-world-basis';
        basis.append(text('summary','查看回应依据'));
        const frozen=payload.reply_basis;
        if(frozen?.status==='available'){
          basis.append(text('p',`最近一次已送达 QQ 回复 · 采用状态的时间：${when(frozen.as_of)}`));
          basis.append(text('p',`当时的活动：${frozen.activity || (frozen.world_used?'没有确定的当前活动':'这次未采用世界状态')}`));
          const names=[...new Set((frozen.reactions || []).map(item=>reactions[item.reaction]).filter(Boolean))];
          basis.append(text('p',`当时的情绪：${names.join('、') || (frozen.emotion_used?'没有已记录的情绪反应':'这次未采用情绪状态')}`));
          for(const item of frozen.reactions || [])if(item.quote)basis.append(text('p',`相关原因：${item.quote}`));
          if(frozen.concerns?.length)basis.append(text('p',`当时挂心的事：${frozen.concerns.join('；')}`));
          basis.append(text('small','这是生成回复时保存的状态，不会用当前世界状态替换。'));
        }else basis.append(text('p',frozen?.status==='unavailable'?'回应依据暂时无法读取。':'最近的 QQ 回复尚无可核验的状态快照，不能用当前状态代替。'));
        status.className='olivia-world-status';status.setAttribute('role','status');
        const scroll=panel.scrollTop;
        panel.replaceChildren(top,overview,tabs,...views,basis,status);
        panel.scrollTop=scroll;
      } else panel.replaceChildren(heading, status, button("更新近况", () => load(true)), now, projects, shared, episodeHistory, moments, historyPanel(), relationship);
    };
    const load = async (refresh = false) => {
      if (busy || !alive()) return;
      busy = true;
      let refreshing = false;
      if (refresh && relationship.open) relationship.refresh();
      try {
        let payload = await (refresh ? requestMutation(DAILY_LIFE_PATH, {}) : requestJson(DAILY_LIFE_PATH));
        if (!alive()) return;
        draw(payload);
        if (!attempted && payload.stale && !payload.refreshing && !payload.error_code) {
          attempted = true;
          payload = await requestMutation(DAILY_LIFE_PATH, {});
          if (!alive()) return;
          draw(payload);
        }
        refreshing = payload.refreshing;
      } catch (_error) {
        if (alive()) {
          status.textContent = "近况暂时无法读取，请稍后重试。";
          if (panel.children.length <= 2) panel.replaceChildren(heading, status, button("重试", () => load()));
        }
      } finally {
        busy = false;
        if (alive()) window.setTimeout(() => load(), refreshing ? 1500 : 60000);
      }
    };
    panel.replaceChildren(heading, status);
    await load();
  };

  const setupInput = (label, type = "text") => {
    const wrapper = document.createElement("label");
    wrapper.style.display = "grid";
    wrapper.style.gap = "6px";
    wrapper.append(text("span", label, "text-text-secondary text-body-m font-regular"));
    const input = document.createElement("input");
    input.type = type;
    input.className = "rounded-3 border border-grey-5 bg-transparent px-4 py-2.5 text-text-body text-body-m";
    input.autocomplete = type === "password" ? "off" : "url";
    input.style.width = "100%";
    wrapper.append(input);
    return { wrapper, input };
  };

  const drawUnifiedStatement = (target, account) => {
    const money=value=>'¥'+new Intl.NumberFormat('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:8}).format(Number(value));
    const status={pending:'预留中',review:'等待结算',reserved:'预留中',settled:'已结算',released:'已释放',rejected:'未收费'};
    target.replaceChildren(text('h4','统一消费账单'));
    target.append(text('p',`可用 ${money(account.remaining_yuan)} · 预留 ${money(account.reserved_yuan)} · 累计消费 ${money(account.used_yuan)}`));
    for(const item of account.items||[]){
      const row=document.createElement('div');row.style.cssText='display:flex;justify-content:space-between;gap:16px;padding:12px 0;border-bottom:1px solid #8884';
      const held=Number(item.reserved_yuan)>0;
      const name=document.createElement('span');name.style.cssText='display:grid;gap:2px;min-width:0';
      name.append(text('span',`${item.label} · ${status[item.status]||item.status}`));
      const at=new Date(item.created_at||'');
      if(!Number.isNaN(at.getTime()))name.append(text('span',at.toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}),'text-text-secondary text-caption-m'));
      row.append(name,text('span',(held?'预留 ':'−')+money(held?item.reserved_yuan:item.charged_yuan)));
      target.append(row);
    }
    if(!(account.items||[]).length)target.append(text('p','还没有消费记录。'));
    const minimum=account.minimum_charge_yuan?`回信中转与 JEV 判断按用量计费，每次最低 ${money(account.minimum_charge_yuan)}，未产生费用的调用不收费。`:'';
    target.append(text('p',minimum+'中转和 GPU 共用同一余额；释放预留不是额外扣款或充值。'+(account.has_more?'当前显示最近 50 笔。':''),'text-text-secondary text-caption-m'));
  };

  const mountRelayAccount = (panel) => {
    panel.style.cssText = "display:grid;gap:20px;min-width:0";
    const account = text("p", "正在读取账户…", "text-text-secondary text-body-m");
    account.setAttribute("role", "status");
    const controls = actions(); controls.style.cssText = "display:flex;flex-wrap:wrap;gap:12px";
    const billing = document.createElement("section");
    const models = document.createElement("section");
    const identity = document.createElement("section");
    identity.className = "olivia-account-key";
    const key = setupInput("我的 Olivia Key", "password");
    key.input.readOnly = true;
    key.input.autocomplete = "off";
    const errors = {RELAY_AUTH_FAILED:"Key 已失效，请检查或联系管理员。", RELAY_ORDER_LIMIT:"申请过于频繁，请稍后重试。", RELAY_NOT_CONFIGURED:"请先申请 Key，或导入已有 Key。"};
    Object.assign(errors, {RELAY_TLS_FAILED:"无法验证回信服务证书，请检查系统时间和网络代理。", RELAY_TIMEOUT:"连接回信服务超时，请稍后重试。", RELAY_CONNECTION_FAILED:"无法连接回信服务，请检查网络或代理。", RELAY_RESPONSE_INVALID:"回信服务返回了无法识别的响应，请稍后重试。"});
    let busy = false;
    const refresh = async () => {
      const result = await requestSetup("/toy/relay/action", {action:"account"});
      account.textContent = result.configured ? "已获取 · 本机加密保存" : result.registration_pending ? "Key 已在本机准备，但服务端注册尚未完成。请点击重试，仍使用同一个 Key。" : "获取专属 Key，开启回信服务。";
      claim.textContent = result.registration_pending ? "重试注册" : "获取 Key";
      claim.hidden = result.configured;
      reveal.hidden = !result.configured;
      key.wrapper.hidden = !result.configured;
      key.input.value = result.key_prefix ? result.key_prefix + "…" : "";
      copy.hidden = !result.configured;
      billing.replaceChildren();
      models.replaceChildren();
      if (result.configured) { mountRelayModels(models); mountRelayBalance(billing); }
    };
    const run = async (action, payload = {}) => {
      if (busy) return;
      busy = true; setButtonsBusy([claim,reveal,copy],true);
      account.textContent = "正在处理…";
      try {
        const result = await requestSetup("/toy/relay/action", {action});
        if (action === "export_key") {
          if (payload.display) { key.input.value=result.key; key.input.type="text"; account.textContent="Key 已显示，请妥善保管。"; }
          else { await navigator.clipboard.writeText(result.key); account.textContent = "Key 已复制，请妥善保存，不要发给他人。"; }
        } else { key.input.value=""; await refresh(); }
      } catch (e) {
        const code=e.name==='AbortError'?'RELAY_TIMEOUT':e instanceof TypeError?'RELAY_LOCAL_CONNECTION_FAILED':/^[A-Z][A-Z0-9_]{0,95}$/.test(e.code||'')?e.code:'RELAY_UNAVAILABLE';
        account.textContent=(errors[code] || "暂时无法完成，请重试。已有账户和余额不会因此丢失。")+`（${code}）`;
      }
      finally {busy=false;setButtonsBusy([claim,reveal,copy],false);}
    };
    const claim = button("获取 Key", () => run("claim"));
    const reveal = button("显示 Key", () => run("export_key", {display:true}));
    const copy = button("复制 Key", () => run("export_key"));
    claim.hidden=reveal.hidden=copy.hidden=true;
    controls.append(claim,reveal,copy);
    identity.append(key.wrapper,controls,account);
    panel.append(identity,models,billing);
    void refresh().catch(()=>{account.textContent="账户读取失败，请关闭后重试。";});
  };

  const mountRelayModels = (panel) => {
    const box = document.createElement("section");
    box.setAttribute("data-olivia-relay-models", "");
    box.style.cssText = "display:grid;gap:12px;margin:24px 0;min-width:0";
    const title = text("h3", "回信模型", "text-text-title text-title-m");
    const description = text("p", "以 Claude Sonnet 5.5 为 1 倍。输入和输出分别计费，实际消费取决于用量；短请求可能受最低计费规则影响。", "text-text-secondary text-body-m");
    const list = document.createElement("fieldset");
    list.style.cssText = "margin:0;padding:0;border:0;min-width:0";
    const legend = text("legend", "选择回信模型");
    legend.style.cssText = "position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)";
    const status = text("p", "正在读取可用模型…", "text-text-secondary text-body-m");
    status.setAttribute("role", "status");
    const controls = actions();
    let active = "", selected = "", busy = false;
    const radios = [];
    const apply = button("使用所选模型", async () => {
      if (busy || !selected || selected === active) return;
      busy = true; apply.disabled = true; radios.forEach(input => { input.disabled = true; });
      status.textContent = "正在切换模型…";
      try {
        const result = await requestSetup("/toy/relay/action", {action:"select_model", model:selected});
        active = result.selected_model;
        status.textContent = "已保存，下一次发送使用所选模型。";
      } catch (_) {
        status.textContent = "切换失败，仍使用原来的模型。请稍后重试。";
      } finally {
        busy = false; apply.disabled = selected === active;
        radios.forEach(input => { input.disabled = false; });
      }
    });
    apply.disabled = true;
    controls.append(apply);
    box.append(title, description, list, controls, status);
    list.append(legend); panel.append(box);
    void requestSetup("/toy/relay/action", {action:"models"}).then(data => {
      if (!box.isConnected) return;
      active = selected = data.selected_model;
      const rows = Array.isArray(data.models) ? data.models : [];
      for (const item of rows) {
        const row = document.createElement("label");
        row.className = "olivia-model-row";
        row.style.cssText = "display:grid;grid-template-columns:20px minmax(0,1fr) auto;align-items:center;gap:12px;padding:14px 0;border-bottom:1px solid #383b42;cursor:pointer";
        const radio = document.createElement("input");
        radio.type = "radio"; radio.name = "olivia-reply-model"; radio.value = item.id;
        radio.checked = item.id === active;
        radio.style.cssText = "margin:0;accent-color:#ded3bd;width:16px;height:16px";
        radio.addEventListener("change", () => { selected = radio.value; apply.disabled = busy || selected === active; });
        radios.push(radio);
        const name = text("span", item.display_name, "text-text-title text-body-m");
        name.style.cssText = "overflow-wrap:anywhere;line-height:1.5";
        const detail = document.createElement("span");
        detail.style.cssText = "display:grid;gap:4px;min-width:0";
        detail.append(name);
        if (item.description) {
          const intro = text("span", item.description, "text-text-secondary text-body-m");
          intro.style.cssText = "font-size:13px;line-height:1.65;overflow-wrap:anywhere";
          detail.append(intro);
        }
        const ratio = value => Number(value).toLocaleString("zh-CN", {maximumFractionDigits:2});
        const ratios = text("span", `输入 ${ratio(item.input_multiplier)}× · 输出 ${ratio(item.output_multiplier)}×`, "text-text-secondary text-body-m");
        ratios.className += " olivia-model-multipliers";
        ratios.style.cssText = "font-variant-numeric:tabular-nums;font-size:13px;line-height:1.5";
        row.append(radio, detail, ratios); list.append(row);
      }
      status.textContent = rows.length ? "选择后点击「使用所选模型」，下一次发送生效。" : "暂无可用模型，请稍后重新打开账户页面。";
    }).catch(() => { status.textContent = "模型列表读取失败，请稍后重新打开账户页面。"; });
  };

  const mountRelayBalance = (panel) => {
    const box = document.createElement("section"); box.setAttribute("data-olivia-relay-billing", "true");
    box.style.cssText = "display:grid;grid-template-columns:minmax(0,1fr);min-width:0;max-width:100%;gap:16px";
    const title = text("h3", "余额与调用量", "text-text-title text-title-m");
    const balance = text("p", "读取已保存的 Olivia 服务账户。", "text-text-secondary text-body-m");
    const usage = text("p", "调用量待读取。", "text-text-secondary text-body-m");
    const metrics = document.createElement("div"); metrics.className="olivia-account-metrics";
    const metric = label => {
      const item=document.createElement("div");
      const value=text("p","—","olivia-metric-value");
      item.append(text("p",label,"olivia-metric-label"),value);metrics.append(item);return value;
    };
    const available=metric("可用余额");
    const spent=metric("累计消费");
    const calls=metric("调用次数");
    const tokens=metric("总 Token");
    const status = text("p", "", "text-text-secondary text-body-m"); status.setAttribute("role", "status");
    const orderView = document.createElement("div"); orderView.style.cssText="display:grid;grid-template-columns:minmax(0,1fr);min-width:0;gap:12px";
    const controls = actions(); controls.style.cssText="display:flex;flex-wrap:wrap;gap:12px;min-width:0";
    let busy = false, active = null, generation = 0, shownSignature = "", pollTimer = null;
    const select = document.createElement("select");
    select.className = "rounded-3 border border-grey-5 bg-transparent px-4 py-2.5 text-text-body text-body-m";
    select.setAttribute("aria-label", "充值面额");
    for (const value of [10,20,50,100]) {
      const option = document.createElement("option"); option.value = String(value*100);
      option.textContent = `充值 ¥${value}`; select.append(option);
    }
    const errors = {RELAY_NOT_CONFIGURED:"请先申请 Key，或导入已有 Key。",
      RELAY_AUTH_FAILED:"Key 已失效，请联系管理员。", RELAY_PHONE_OFFLINE:"收款服务暂时离线，请稍后再充值。",
      RELAY_ORDER_LIMIT:"创建订单过于频繁，请稍后重试。", RELAY_SLOTS_FULL:"当前充值人数较多，请稍后重试。"};
    const call = payload => requestSetup("/toy/relay/action", payload);
    const enabled = () => { refresh.disabled=busy; create.disabled=busy || Boolean(active); select.disabled=busy || Boolean(active); };
    const queuePoll = serial => {
      if (pollTimer !== null) window.clearTimeout(pollTimer);
      pollTimer = window.setTimeout(async () => {
        if (!box.isConnected || serial !== generation) return;
        try { const next=await call({action:"order_status"}); if (!box.isConnected || serial !== generation) return;
          drawOrder(next); if (next.order?.state === "credited") await readBalance();
        } catch (_) { if(box.isConnected && serial === generation) status.textContent="到账状态暂时无法更新，请点击刷新。请勿重复付款。"; }
      },3000);
    };
    const drawOrder = data => {
      const order = data.order;
      const signature = JSON.stringify(order && [order.id, order.state, order.expires_at]);
      // Keep the pending controls mounted so polling cannot steal keyboard focus.
      if (order && order.state === "pending" && signature === shownSignature) { queuePoll(generation); return; }
      shownSignature = signature; orderView.replaceChildren(); active = null;
      const serial = ++generation;
      if (!order) { enabled(); return; }
      const credit = Number(order.credit_yuan).toFixed(2);
      if (order.state === "credited") {
        status.textContent = `充值已到账 ¥${credit}。`;
        orderView.append(text("p", `已到账 ¥${credit}。`, "text-text-title text-body-m")); enabled(); return;
      }
      if (order.state === "expired") {
        status.textContent = "付款金额已过期，请勿继续付款。";
        orderView.append(text("p", "此付款金额已过期，请勿继续付款。已付款但未到账，请联系管理员核对。", "text-text-secondary text-body-m")); enabled(); return;
      }
      active = order;
      select.value = String(Math.round(Number(order.credit_yuan)*100));
      const amount = "¥"+(order.amount_cents/100).toFixed(2);
      const pay = text("p", `请用微信支付 ${amount}`, "text-text-title text-title-m");
      pay.style.cssText="font-size:24px;font-weight:600;line-height:1.4;font-variant-numeric:tabular-nums";
      const countdown = text("p", "", "text-text-secondary text-body-m");
      countdown.setAttribute("aria-live", "off");
      const qr = document.createElement("img");
      qr.src = "__OLIVIA_WECHAT_PAYMENT_QR__";
      qr.alt = "微信收款码，收款人 Ornn，请按订单显示金额付款";
      qr.style.cssText = "display:block;width:280px;max-width:100%;height:auto;margin:12px auto;border-radius:12px";
      const paid = text("p", `请用微信扫描下方收款码，准确支付 ${amount}（含小数），请勿取整。到账余额 ¥${credit}。`, "text-text-secondary text-body-m");
      orderView.append(pay, paid, qr, countdown);
      status.textContent = `付款金额 ${amount}，到账余额 ¥${credit}。`;
      const deadline = Date.now() + Math.max(0, order.expires_at-data.server_time)*1000;
      const tick = () => {
        if (!box.isConnected || serial !== generation) return;
        const seconds = Math.max(0, Math.ceil((deadline-Date.now())/1000));
        countdown.textContent = `有效时间 ${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,"0")} · 正在等待微信到账通知`;
        if (!seconds) { pay.textContent="付款金额已过期，请勿继续付款。"; qr.remove(); paid.hidden=true; active=null; enabled(); return; }
        window.setTimeout(tick,1000);
      };
      tick(); enabled();
      queuePoll(serial);
    };
    const unifiedHistory=document.createElement("section");
    const readBalance = async () => {
      const data = await call({action:"balance"});
      if (!box.isConnected) return;
      if (data.billing_mode !== "money") throw {error_code:"RELAY_NOT_CONFIGURED"};
      balance.textContent = `可用余额 ¥${Number(data.remaining_yuan).toFixed(4)} · 累计消费 ¥${Number(data.used_yuan).toFixed(4)}`;
      balance.hidden=true;
      available.textContent=`¥${Number(data.remaining_yuan).toFixed(4)}`;
      spent.textContent=`¥${Number(data.used_yuan).toFixed(4)}`;
      try {drawUnifiedStatement(unifiedHistory,await call({action:'statement'}));} catch(_){unifiedHistory.textContent='统一账单暂时无法读取，请稍后刷新。';}
      const counts = data.usage;
      calls.textContent=counts ? counts.calls.toLocaleString() : "—";
      tokens.textContent=counts ? counts.total_tokens.toLocaleString() : "—";
      usage.textContent = counts ? `输入 ${counts.input_tokens.toLocaleString()} / 输出 ${counts.output_tokens.toLocaleString()} Token · 仅统计已结算调用` : "调用量暂不可用，请稍后刷新。";
    };
    const run = async work => { if(busy)return;busy=true;enabled();status.textContent="正在读取…";
      try { await work();if(status.textContent==="正在读取…")status.textContent=""; }
      catch(e) {status.textContent=errors[e.code || e.error_code] || "服务暂时无法连接，请重试；已付款请勿重复支付。";}
      finally {busy=false;enabled();}
    };
    const refresh = button("刷新", () => run(async()=>{await readBalance();drawOrder(await call({action:"order_status"}));}));
    const create = button("获取付款金额", () => run(async()=>{await readBalance();drawOrder(await call({action:"create_order",amount_cents:Number(select.value)}));}));
    controls.append(select,create,refresh);
    create.className += " olivia-primary-action";
    const recharge = text("h3","账户充值","text-text-title text-title-m");
    recharge.style.marginTop="12px";
    box.append(title,metrics,balance,usage,unifiedHistory,recharge,controls,orderView,status);
    panel.append(box);
    void run(async()=>{await readBalance();drawOrder(await call({action:"order_status"}));});
    const refreshVisibleBalance = async () => {
      if (!box.isConnected) return;
      if (!document.hidden && box.getClientRects().length && !busy) {
        busy=true;
        try { await readBalance(); } catch (_) { /* Keep the last confirmed balance on network failure. */ }
        finally { busy=false; }
      }
      if (box.isConnected) window.setTimeout(refreshVisibleBalance,15000);
    };
    window.setTimeout(refreshVisibleBalance,15000);
  };

  const renderLlmSetupPanel = async (panel, initialMode) => {
    panel.replaceChildren(
      text("h3", "连接回信服务", "text-text-title text-title-m"),
      text("p", "Olivia 只使用 Olivia 账户 Key。Key 仅加密保存在这台电脑上，不会显示在页面或日志中。", "text-text-secondary text-body-m font-regular")
    );
    let setup;
    try {
      setup = await requestSetup(SETUP_STATUS_PATH);
    } catch (_error) {
      panel.append(text("p", "初始设置服务暂不可用。", "text-text-secondary text-body-m font-regular"));
      return;
    }
    const key = setupInput("导入已有的 Olivia Key（已获取 Key 时留空）", "password");
    key.input.maxLength = 128;
    key.input.value = "";
    const state = text("p", setup.llm.key_configured
      ? "已找到保存的 Key，无需重新填写。"
      : "还没有 Key？请先在「Olivia 账户」获取，或在上方粘贴已有的 Key。", "text-text-secondary text-body-m font-regular");
    state.setAttribute("aria-live", "polite");
    let setupBusy = false;
    const errors = {RELAY_NOT_CONFIGURED:"请先在「Olivia 账户」获取 Key，或粘贴已有的 Key。",
      LLM_SETUP_FIELDS_INVALID:"Key 格式不正确，应以 olivia- 开头。",
      RELAY_AUTH_FAILED:"Key 已失效，请检查或联系管理员。"};
    const connect = button("连接并保存", async () => {
      if (setupBusy) return;
      const typed = key.input.value.trim();
      setupBusy = true;
      setButtonsBusy([connect, removeKey], true);
      state.textContent = "正在连接 Olivia 回信服务…";
      try {
        await requestSetup("/toy/relay/action", typed ? {action:"import_key", key:typed} : {action:"connect"});
        key.input.value = "";
        setup.llm.key_configured = true;
        removeKey.hidden = false;
        state.textContent = "已连接并保存 Olivia 回信服务，下一次发送生效。";
      } catch (e) {
        state.textContent = errors[e.code] || "连接失败，请检查 Key 或稍后重试。";
      } finally {
        setupBusy = false;
        setButtonsBusy([connect, removeKey], false);
      }
    });
    const removeKey = button("删除 Key", async () => {
      if (setupBusy || !await confirmAction("确认删除这台电脑上保存的回信服务连接？")) {
        return;
      }
      setupBusy = true;
      setButtonsBusy([connect, removeKey], true);
      try {
        await requestSetup(LLM_DELETE_PATH, {});
        key.input.value = "";
        state.textContent = "连接已删除。下一次发送立即生效。";
      } catch (_error) {
        state.textContent = "删除失败，请重试。";
      } finally {
        setupBusy = false;
        setButtonsBusy([connect, removeKey], false);
      }
    });
    removeKey.hidden = !setup.llm.key_configured || initialMode;
    const controls = actions();
    controls.append(connect, removeKey);
    panel.append(key.wrapper, controls, state);
  };

  const formatBytes = (value) => {
    if (!Number.isInteger(value) || value < 0) return "未知";
    if (value < 1024 * 1024) return `${Math.ceil(value / 1024)} KiB`;
    if (value >= 1024 * 1024 * 1024) {
      return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GiB`;
    }
    return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
  };

  const renderMem0CapabilityPanel = async (panel, initialMode = false) => {
    let payload;
    try {
      payload = await requestCapability(MEM0_CAPABILITY_PATH);
    } catch (_error) {
      panel.replaceChildren(
        text("h3", "长期记忆", "text-text-title text-title-m"),
        text("p", "下载管理服务暂不可用。", "text-text-secondary text-body-m font-regular")
      );
      return;
    }
    const allowedStates = ["missing", "queued", "downloading", "verifying", "ready", "paused", "repair", "incompatible"];
    const stateValue = allowedStates.includes(payload.state) ? payload.state : "repair";
    const offlineImport = ["offline", "offline-package"].includes(payload.source);
    const runtimePreparing = ["queued", "downloading", "verifying"].includes(stateValue)
      && ["python-runtime-preparation", "python-dependencies"].includes(payload.current_file);
    if (runtimePreparing && mem0RuntimeProgressStartedAt === null) {
      mem0RuntimeProgressStartedAt = Date.now();
    } else if (!runtimePreparing) {
      mem0RuntimeProgressStartedAt = null;
    }
    const runtimeElapsedSeconds = runtimePreparing
      ? Math.max(0, Math.floor((Date.now() - mem0RuntimeProgressStartedAt) / 1000))
      : 0;
    let runtimeLoaded = false;
    if (stateValue === "ready") {
      try {
        const companion = await requestJson(STATUS_PATH);
        runtimeLoaded = companion && companion.capabilities
          && companion.capabilities.memory
          && companion.capabilities.memory.state === "available";
      } catch (_error) {
        runtimeLoaded = false;
      }
    }
    const labels = {
      missing: "未安装",
      queued: offlineImport ? "等待导入" : "等待下载",
      downloading: offlineImport ? "正在校验并导入离线包" : "下载中",
      verifying: "校验中",
      ready: runtimeLoaded ? "记忆已准备好，可以继续连接回信服务" : "记忆包已安装，正在准备记忆服务",
      paused: "已暂停",
      repair: "需修复",
      incompatible: "不兼容",
    };
    const heading = text("h3", "长期记忆", "text-text-title text-title-m");
    const summary = text(
      "p",
      "选择已下载的记忆包 ZIP，无需解压。已有记忆包会自动识别，无需重复导入。",
      "text-text-secondary text-body-m font-regular"
    );
    const metadata = stack();
    metadata.append(
      field("状态", labels[stateValue]),
      field(offlineImport ? "离线包内容" : "下载量", formatBytes(payload.total_bytes)),
      field(offlineImport ? "待处理" : "剩余", formatBytes(payload.remaining_bytes)),
      field("安装后占用", formatBytes(payload.installed_bytes)),
      field("实际来源", typeof payload.source === "string" ? payload.source : "尚未选择"),
      field("运行设备", payload.requires_gpu === false ? "CPU（无需 GPU）" : "请查看兼容说明")
    );
    const result = text("p", "", "text-text-secondary text-body-m font-regular");
    result.setAttribute("aria-live", "polite");
    if (["queued", "downloading", "verifying"].includes(stateValue)) {
      const currentFile = runtimePreparing
        ? `正在准备运行环境，已用时 ${runtimeElapsedSeconds} 秒（首次约需 3–8 分钟）`
        : payload.current_file;
      const current = typeof currentFile === "string" ? `，当前：${currentFile}` : "";
      result.textContent = `${labels[stateValue]}：${formatBytes(payload.downloaded_bytes)} / ${formatBytes(payload.total_bytes)}，${offlineImport ? "待处理" : "剩余"} ${formatBytes(payload.remaining_bytes)}${current}`;
    } else if (stateValue === "repair") {
      result.textContent = offlineImport
        ? "上次离线导入未完成，请重新选择完整离线包。"
        : "上次安装未完成，可保留已下载内容并重试。";
    }
    const controls = actions();
    const refresh = async () => {
      if (panel.isConnected === false) return;
      await renderMem0CapabilityPanel(panel, initialMode);
    };
    if (["queued", "downloading", "verifying"].includes(stateValue)) {
      const pause = button(offlineImport ? "暂停导入" : "暂停下载", async () => {
        try {
          await requestCapability(MEM0_CAPABILITY_ACTION_PATH, { action: "pause" });
          await refresh();
        } catch (_error) {
          result.textContent = "暂时无法暂停，请稍后重试。";
        }
      });
      controls.append(pause);
      window.setTimeout(refresh, 1000);
    } else if (stateValue === "ready" && !initialMode) {
      const uninstall = button("卸载运行依赖", async () => {
        if (!await confirmAction("确认卸载长期记忆运行依赖？已下载模型和个人记忆会保留。")) return;
        await requestCapability(MEM0_CAPABILITY_ACTION_PATH, {
          action: "uninstall",
          remove_model: false,
        });
        await refresh();
      });
      const removeAll = button("卸载并删除模型", async () => {
        if (!await confirmAction("确认卸载长期记忆并删除已下载模型？个人记忆仍会保留。")) return;
        if (!await confirmAction("模型删除后重新启用需要再次导入离线包，仍要继续吗？")) return;
        await requestCapability(MEM0_CAPABILITY_ACTION_PATH, {
          action: "uninstall",
          remove_model: true,
        });
        await refresh();
      });
      controls.append(uninstall, removeAll);
    }
    if (stateValue === "ready" && !runtimeLoaded) {
      const retry = button("重新准备记忆服务", async () => {
        setButtonsBusy([retry], true);
        try {
          await requestMutation(MEMORY_RETRY_PATH, {});
          await refresh();
        } catch (_error) {
          result.textContent = "记忆服务还未准备完成，已安装的记忆包会保留。请稍候重试。";
        } finally {
          setButtonsBusy([retry], false);
        }
      });
      controls.append(retry);
      window.setTimeout(refresh, 2000);
    }
    if (["missing", "repair", "paused"].includes(stateValue)) {
      const importOffline = button("导入记忆离线包（ZIP）", async () => {
        setButtonsBusy([importOffline], true);
        result.textContent = "请选择 Olivia 记忆离线包（ZIP），无需解压。";
        try {
          const response = await requestCapability(
            MEM0_CAPABILITY_ACTION_PATH,
            { action: "import_offline" }
          );
          if (response.status === "CANCELLED") {
            result.textContent = "已取消导入。";
            return;
          }
          await refresh();
        } catch (_error) {
          result.textContent = "离线包导入未能启动，请重新选择完整 ZIP。";
        } finally {
          setButtonsBusy([importOffline], false);
        }
      });
      controls.append(importOffline);
    }
    if (initialMode) {
      const details = document.createElement("details");
      details.append(text("summary", "安装详情"), metadata);
      panel.replaceChildren(heading, summary, text("p", labels[stateValue]), controls, result, details);
    } else {
      panel.replaceChildren(heading, summary, metadata, controls, result);
    }
    if (payload.reason_code) setDiagnosticDetails(result, payload.reason_code);
  };

  const renderLocalUpdatePanel = (panel) => {
    const heading = text("h3", "本地补丁", "text-text-title text-title-m");
    const summary = text(
      "p",
      "下载我们发布的更新 ZIP，直接选择即可校验并安装，无需解压、联网获取校验码或手动填写。也支持原来的 .oliviapatch 文件。安装后关闭并重新打开 Olivia 生效。",
      "text-text-secondary text-body-m font-regular"
    );
    let packagePath = "";
    const selectedPatch = text(
      "p",
      "尚未选择补丁文件。",
      "text-text-secondary text-body-m font-regular"
    );
    const digest = setupInput("发布说明提供的 Manifest SHA-256");
    digest.input.maxLength = 64;
    digest.input.autocomplete = "off";
    const result = text("p", "", "text-text-secondary text-body-m font-regular");
    result.setAttribute("aria-live", "polite");
    const choose = button("选择补丁并更新", async () => {
      setDiagnosticDetails(result, []);
      setButtonsBusy([choose, install, rollback], true);
      result.textContent = "请选择已下载的补丁；选中后将自动校验并安装。";
      try {
        const payload = await requestUpdate({ action: "select" });
        if (payload.status === "SELECTED" && typeof payload.package_path === "string") {
          packagePath = payload.package_path;
          selectedPatch.textContent = `已选择：${packagePath.split(/[\\/]/).pop()}`;
          result.textContent = /\.zip$/i.test(packagePath) ? "正在校验更新 ZIP 并安装……" : "正在获取官方校验值并安装补丁……";
          const applied = await requestUpdate({ action: "apply_verified", package_path: packagePath });
          result.textContent = `版本 ${applied.version} 已安装，关闭并重新打开 Olivia 后生效。`;
        } else if (payload.status === "CANCELLED") {
          result.textContent = "已取消，本次未安装补丁。";
        }
      } catch (error) {
        const code = error && error.code ? error.code : "UPDATE_ACTION_UNAVAILABLE";
        result.textContent = code === "UPDATE_CHECKSUM_UNAVAILABLE"
          ? "无法获取官方校验值，尚未安装。请检查网络后重试，或展开手动校验，填入发布说明中的校验值。"
          : code === "UPDATE_BUNDLE_INVALID" || code === "UPDATE_BUNDLE_CHECKSUM_MISMATCH"
          ? "更新 ZIP 不完整、结构不正确或校验不匹配，尚未安装。请重新下载我们发布的更新 ZIP。"
          : "本次更新未完成。请关闭其他 Olivia 窗口后重试，或重新选择完整的更新包。";
        setDiagnosticDetails(result, code);
      } finally {
        setButtonsBusy([choose, install, rollback], false);
      }
    });
    const install = button("手动校验并安装", async () => {
      setDiagnosticDetails(result, []);
      const manifestSha256 = digest.input.value.trim().toLowerCase();
      if (!packagePath) {
        result.textContent = "请选择已下载的 .oliviapatch 文件。";
        return;
      }
      if (!/^[0-9a-f]{64}$/.test(manifestSha256)) {
        result.textContent = "请输入发布说明提供的 64 位 Manifest SHA-256。";
        return;
      }
      if (!await confirmAction("确认校验并安装这个本地补丁？")) return;
      setButtonsBusy([choose, install, rollback], true);
      result.textContent = "正在校验并安装补丁……";
      try {
        const payload = await requestUpdate({
          action: "apply",
          package_path: packagePath,
          manifest_sha256: manifestSha256,
        });
        result.textContent = `版本 ${payload.version} 已安装，关闭并重新打开 Olivia 后生效。`;
      } catch (error) {
        result.textContent = "补丁未能安装。请确认更新包已下载完整、校验值填写正确，然后重试。";
        setDiagnosticDetails(result, error?.code || "UPDATE_ACTION_UNAVAILABLE");
      } finally {
        setButtonsBusy([choose, install, rollback], false);
      }
    });
    const rollback = button("回滚上一版本", async () => {
      setDiagnosticDetails(result, []);
      if (!await confirmAction("确认回滚到上一版本？关闭并重新打开 Olivia 后生效。")) return;
      setButtonsBusy([choose, install, rollback], true);
      result.textContent = "正在切换到上一版本……";
      try {
        const payload = await requestUpdate({ action: "rollback" });
        result.textContent = `已回滚到版本 ${payload.version}，关闭并重新打开 Olivia 后生效。`;
      } catch (error) {
        result.textContent = "暂时无法恢复上一版本。请关闭其他 Olivia 窗口后重试；如果没有上一版本，可导入新的修复补丁。";
        setDiagnosticDetails(result, error?.code || "UPDATE_ACTION_UNAVAILABLE");
      } finally {
        setButtonsBusy([choose, install, rollback], false);
      }
    });
    const controls = actions();
    controls.append(choose);
    const manual = document.createElement("details");
    manual.className = "olivia-group-advanced";
    const rollbackRow = actions();
    rollbackRow.append(rollback);
    manual.append(text("summary", "高级：手动校验补丁或回滚上一版本", "text-text-secondary text-body-m"),
      digest.wrapper, install, rollbackRow);
    panel.replaceChildren(
      heading,
      summary,
      selectedPatch,
      controls,
      manual,
      result
    );
  };

  const loadDialogData = async (statusNode, panels, initialMode) => {
    if (panels.memory) panels.memory.__oliviaCompanionStatusNode = statusNode;
    const tasks = [
      ...(panels.llm ? [renderLlmSetupPanel(panels.llm, initialMode)] : []),
      ...(initialMode ? [renderMem0CapabilityPanel(panels.capability, true)] : []),
    ];
    if (initialMode) {
      statusNode.textContent = "先导入记忆包，再连接回信服务，即可开始写信。已有配置会自动沿用；语音、图片和视频无需在这里安装。";
      await Promise.allSettled(tasks);
      return;
    }
    if (panels.update) tasks.push(Promise.resolve(renderLocalUpdatePanel(panels.update)));
    statusNode.textContent = "正在连接本机陪伴服务……";
    try {
      const payload = await requestJson(STATUS_PATH);
      const capabilities = payload.capabilities && typeof payload.capabilities === "object"
        ? payload.capabilities
        : {};
      renderCompanionStatus(statusNode, capabilities);
      await Promise.allSettled(tasks.concat([
        renderMemoryPanel(panels.memory, capabilities.memory),
        renderPrivateWorldPanel(panels.privateWorld, capabilities.private_world),
      ]));
    } catch (error) {
      statusNode.textContent = error && error.name === "AbortError"
        ? "陪伴状态查询超时，其他功能将独立检查；可重新打开此窗口重试。"
        : "陪伴状态读取失败，其他功能将独立检查；可导出诊断包排查。";
      statusNode.dataset.state = "unavailable";
      await Promise.allSettled(tasks.concat([
        renderMemoryPanel(panels.memory, {state: "available"}),
        renderPrivateWorldPanel(panels.privateWorld, {state: "available"}),
      ]));
    }
  };

  // Local performance import/catalog design: 芙桃, used with permission.
  const localSongRequest = async (action = "", body = null) => {
    const response = await fetch(new URL("/toy/local-songs" + action, apiBase), {
      method: body ? "POST" : "GET", cache: "no-store", credentials: "omit",
      headers: {"Content-Type": "application/json", [CONFIRM_HEADER]: CONFIRM_VALUE},
      ...(body ? {body: JSON.stringify(body)} : {}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || "LOCAL_SONG_UNAVAILABLE");
    return result.data;
  };
  const refreshLocalSongCatalog = async () => {
    const catalog = window.__oliviaLocalSongCatalog;
    if (!catalog) return;
    const result = await localSongRequest();
    const imported = result.songs.map((song) => {
      const isAudio=song.media_type === "audio";
      const url = new URL(`/toy/local-songs/media/${song.id}.${isAudio ? "wav" : "mp4"}`, apiBase).href;
      return {
        id: String(1000000000000000 + parseInt(song.id.slice(0, 12), 16)),
        itemId: String(1000000000000000 + parseInt(song.id.slice(0, 12), 16)), itemType: 3, name: song.name,
        nameKey: "local_" + song.id, styleType: "Local Performance",
        styleTypeDisplayName: "本地演奏", performanceType: "Solo", source: "songlist",
        videoUrl: isAudio ? "" : url, mediaUrl: url, coverUrl: "", iconUrl: "", audioUrl: isAudio ? url : "",
        duration: song.duration, videoDuration: song.duration, audioDuration: song.duration,
        videoByTodView: isAudio ? [] : [{url, tod: "TOD12", view: "NI", coverUrl: "", duration: Math.round(song.duration)}], oliviaLocal: true,
      };
    });
    if (window.cefViewQuery && imported.length) {
      const native = (action, data) => new Promise((resolve, fail) => {
        const timer = setTimeout(() => fail(new Error("LOCAL_SONG_NATIVE_TIMEOUT")), 15000);
        window.cefViewQuery({request: JSON.stringify({action, data}),
          onSuccess: (raw) => {
            clearTimeout(timer);
            try { resolve(typeof raw === "string" && raw ? JSON.parse(raw) : raw); }
            catch (error) { fail(error); }
          },
          onFailure: () => { clearTimeout(timer); fail(new Error("LOCAL_SONG_NATIVE_FAILED")); },
        });
      });
      const check = () => native("checkLocalSongs", {songs: imported.map((song, index) => ({...song, eventId: String(index + 1)}))});
      const status = await check();
      const missing = imported.filter((song) => !status.songs?.some((item) => String(item.songId) === song.id && item.exist));
      if (missing.length) {
        await native("startSongDownload", {songs: missing});
        let ready = false;
        for (let attempt = 0; attempt < 120; attempt++) {
          await new Promise((resolve) => setTimeout(resolve, 500));
          const current = await check();
          if (imported.every((song) => current.songs?.some((item) => String(item.songId) === song.id && item.exist))) { ready = true; break; }
        }
        if (!ready) throw new Error("LOCAL_SONG_NATIVE_CACHE_NOT_READY");
      }
    }
    catalog.songs.value = [...catalog.songs.value.filter((song) => !song.oliviaLocal), ...imported];
    catalog.musicStyles.value = [...catalog.musicStyles.value.filter((style) => style.type !== "Local Performance"),
      {type: "Local Performance", displayName: "本地演奏"}];
  };
  window.addEventListener("olivia-local-catalog-ready", () => {
    refreshLocalSongCatalog().catch(() => {});
  });
  let localSongImportPending = null;
  const renderLocalSongs = async (panel) => {
    panel.replaceChildren();
    panel.append(text("h3", "本地演奏", "text-text-title text-title-m"),
      text("p", "恢复以前的 MIDI 演奏视频，或导入本地视频到曲库。", "text-text-secondary"));
    const path = document.createElement("input");
    path.type = "text";
    path.placeholder = "粘贴视频文件或演奏文件夹的完整路径";
    path.setAttribute("aria-label", "本地演奏路径");
    Object.assign(path.style, {width: "100%", padding: "10px", background: "#202123", color: "inherit", border: "1px solid #606164", borderRadius: "8px"});
    const state = text("p", "支持 MP4、MOV、MKV 等视频；每个原版 midi_* 文件夹恢复主视频。", "text-text-secondary");
    state.setAttribute("aria-live", "polite");
    const list = stack();
    const reload = async () => {
      const result = await localSongRequest();
      list.replaceChildren();
      for (const song of result.songs) {
        const row = card();
        row.style.background = "#202123";
        const name = document.createElement("input");
        name.value = song.name; name.maxLength = 120;
        name.setAttribute("aria-label", "曲名");
        Object.assign(name.style, {background: "transparent", color: "inherit", padding: "8px", border: "1px solid #606164", borderRadius: "8px"});
        const controls = actions();
        controls.append(button("播放", () => {
          let video = row.querySelector("video, audio");
          if (!video) {
            video = document.createElement(song.media_type === "audio" ? "audio" : "video"); video.controls = true;
            video.src = new URL(`/toy/local-songs/media/${song.id}.${song.media_type === "audio" ? "wav" : "mp4"}`, apiBase).href;
            video.style.width = "100%"; row.append(video);
          }
          video.play().catch(() => {});
        }), button("保存曲名", async () => {
          try {
            await localSongRequest("/rename", {id: song.id, name: name.value});
            await refreshLocalSongCatalog(); state.textContent = "曲名已保存。";
          } catch (_error) { state.textContent = "曲名保存失败，请重试。"; }
        }), button("删除", async () => {
          if (!await confirmAction("从本地曲库删除这段演奏？原始文件会保留。")) return;
          const video = row.querySelector("video, audio");
          if (video) { video.pause(); video.removeAttribute("src"); video.load(); }
          try {
            await localSongRequest("/delete", {id: song.id});
            await reload(); await refreshLocalSongCatalog(); state.textContent = "已删除。";
          } catch (_error) { state.textContent = "删除失败；若正在播放，请停止播放后重试。"; }
        }));
        controls.append(button("打开文件位置", async () => {
          try {
            await localSongRequest("/reveal", {id: song.id});
            state.textContent = "已在资源管理器中定位文件。";
          } catch (_error) { state.textContent = "无法打开文件位置，请确认文件仍在本地后重试。"; }
        }));
        row.append(name, text("span", `${Math.round(song.duration)} 秒`), controls); list.append(row);
      }
      if (!result.songs.length) list.append(text("p", "还没有导入演奏。"));
    };
    const importButton = button("导入到曲库", async () => {
      if (localSongImportPending) return;
      const value = path.value.trim().replace(/^"|"$/g, "");
      if (!value) { state.textContent = "请填写文件或文件夹路径。"; return; }
      localSongImportPending = localSongRequest("/import", {path: value});
      await waitForImport();
    });
    const waitForImport = async () => {
      importButton.disabled = true; state.textContent = "正在导入，必要时会转换视频格式，请稍候……";
      const pending = localSongImportPending;
      try {
        const result = await pending;
        state.textContent = `已导入 ${result.added} 段，跳过重复 ${result.skipped} 段，失败 ${result.failed} 段。`;
        if (result.errors.length) {
          state.textContent += result.errors.slice(0, 3).map((error) => `${error.name}：${error.code}`).join("；");
          if (result.errors.length > 3) state.textContent += `；另有 ${result.errors.length - 3} 项失败。`;
          state.textContent += " 请导出诊断包以查看具体原因。";
        }
        await reload(); await refreshLocalSongCatalog();
      } catch (_error) {
        state.textContent = _error.message === "LOCAL_SONG_FFMPEG_UNAVAILABLE"
          ? "导入已停止：未找到 FFmpeg。请在本地组件中导入新版媒体工具组件，重启后重试。原视频已保留。"
          : `导入失败（${/^[A-Z][A-Z0-9_]{0,95}$/.test(_error.message) ? _error.message : "LOCAL_SONG_UNAVAILABLE"}），请检查路径、媒体工具和磁盘空间后重试。`;
      }
      finally { if (localSongImportPending === pending) localSongImportPending = null; importButton.disabled = false; }
    };
    const pickButton = button("选择文件夹", async () => {
      if (!window.cefViewQuery) {
        state.textContent = "请在上方粘贴文件夹的完整路径。"; return;
      }
      pickButton.disabled = true;
      try {
        const picked = await new Promise((resolve, fail) => window.cefViewQuery({
          request: JSON.stringify({action: "showFileDirectoryPicker", data: {type: "directory", needAvailableSpace: false}}),
          onSuccess: (raw) => {
            try { resolve(typeof raw === "string" ? JSON.parse(raw) : raw); }
            catch (error) { fail(error); }
          },
          onFailure: fail,
        }));
        const selected = picked && (picked.path || picked.dir || picked.folder);
        if (typeof selected === "string" && selected) {
          path.value = selected; state.textContent = "已选择文件夹，点击导入到曲库开始导入。";
        } else { state.textContent = "已取消选择。"; }
      } catch (_error) { state.textContent = "无法打开文件夹选择窗口，请粘贴完整路径。"; }
      finally { pickButton.disabled = false; }
    });
    const importActions = actions(); importActions.append(pickButton, importButton);
    panel.append(path, importActions, state, list,
      text("p", "本地演奏导入功能参考：芙桃（已授权）。", "text-text-secondary"));
    try { await reload(); } catch (_error) { state.textContent = "曲库读取失败，请稍后重试。"; }
    if (localSongImportPending) await waitForImport();
  };

  const openLocalSongs = () => {
    if (document.querySelector('[data-olivia-local-songs-dialog]')) return;
    const backdrop = document.createElement("div");
    backdrop.dataset.oliviaLocalSongsDialog = "";
    Object.assign(backdrop.style, {position: "fixed", inset: "0", zIndex: "2147483000",
      display: "grid", placeItems: "center", background: "rgba(0,0,0,.62)", WebkitAppRegion: "no-drag"});
    const dialog = document.createElement("section");
    dialog.setAttribute("role", "dialog"); dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-label", "本地演奏");
    Object.assign(dialog.style, {width: "min(720px,calc(100vw - 80px))", maxHeight: "85vh",
      overflow: "auto", padding: "28px", borderRadius: "16px", background: "#18191b", color: "#dcd7cf"});
    const close = button("关闭", () => backdrop.remove());
    const header = actions(); header.style.justifyContent = "flex-end"; header.append(close);
    const panel = stack(); dialog.append(header, panel); backdrop.append(dialog);
    backdrop.addEventListener("click", (event) => { if (event.target === backdrop) backdrop.remove(); });
    backdrop.addEventListener("keydown", (event) => { if (event.key === "Escape") backdrop.remove(); });
    document.body.append(backdrop); close.focus(); renderLocalSongs(panel);
  };
  const mountLocalSongEntry = () => {
    if (window.location.hash.split("?")[0] !== "#/studio") {
      document.querySelector('[data-olivia-local-songs-entry]')?.remove(); return;
    }
    if (document.querySelector('[data-olivia-local-songs-entry]')) return;
    const navigation = document.querySelector('[data-olivia-main-navigation]');
    if (!navigation) return;
    const entry = button("导入本地演奏", openLocalSongs);
    entry.dataset.oliviaLocalSongsEntry = "";
    Object.assign(entry.style, {whiteSpace: "nowrap", flexShrink: "0", minHeight: "36px"});
    navigation.append(entry);
  };

  const isSettingsRoute = () => {
    const route = `${window.location.pathname} ${window.location.hash}`;
    return /(?:^|[\/#])settings(?:[\/?#]|$)/i.test(route);
  };

  const removeShell = () => {
    document.querySelector(`[${ROOT_ATTR}]`)?.remove();
  };

  // The lite client's mailbox is the original /collection view. Keep both
  // destinations inside the main window: desktop widgets can be off-screen.
  const WORLD_ROUTE = '#/world';
  const mountWorldPage = (page) => {
    page.dataset.oliviaWorldPage='';page.setAttribute('aria-label','世界');
    const style=document.createElement('style');style.textContent=`
      [data-olivia-world-page]{width:100%;height:100%;min-height:0;color:#ded9d1;display:flex;flex-direction:column;gap:24px;-webkit-app-region:no-drag}
      .olivia-world-header{height:40px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
      .olivia-world-header h1{font-size:30px;margin:0;font-weight:700}
      [data-world-main]{overflow-y:auto;overflow-x:hidden;background:#191a1c;border-radius:12px;padding:28px 32px;min-height:0;flex:1;display:block;box-sizing:border-box;scrollbar-width:thin}
      [data-world-main] p{line-height:1.7;margin:8px 0}
      [data-world-main] h3{font-size:26px;margin:0}[data-world-main] h4{font-size:21px;margin:0}
      [data-world-main] button,.olivia-world-header button{border:1px solid #686a70;border-radius:999px;background:transparent;color:#ded9d1;padding:9px 18px;font:inherit;cursor:pointer}
      [data-world-main] summary{cursor:pointer;line-height:1.7}
      [data-world-main] article{background:transparent!important;padding:12px 0!important}
      .olivia-world-heading{display:flex;justify-content:space-between;align-items:center;gap:16px}
      .olivia-world-columns{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(0,1fr);gap:32px}
      .olivia-world-columns>*{min-width:0;margin-top:0!important}
      .olivia-world-columns>section{align-content:start}
      .olivia-world-aside{border-left:1px solid #383a3e;padding-left:28px;min-width:0}
      .olivia-world-aside>section{margin-top:0!important}
      .olivia-world-heading{font-size:14px;color:#acb0b4}
      .olivia-world-heading button{flex-shrink:0;font-size:13px}
      .olivia-world-overview{padding:22px 0 0}
      .olivia-world-meta{display:flex;align-items:center;flex-wrap:wrap;gap:16px;font-size:13px;color:#acb0b4;margin-top:10px}
      .olivia-world-line,.olivia-world-agenda{display:flex;gap:12px;align-items:center;overflow-wrap:anywhere}
      [data-world-main] svg{width:20px;height:20px;flex-shrink:0;fill:none;stroke:currentColor;stroke-width:1.65;stroke-linecap:round;stroke-linejoin:round}
      .olivia-world-meta svg{width:16px;height:16px}
      .olivia-world-mood{margin-top:22px;display:grid;gap:8px;font-size:15px}
      .olivia-world-mood>.olivia-world-line svg{color:#b6c8b0}
      [data-world-main] .olivia-world-muted{color:#acb0b4;font-size:14px}
      [data-world-main] .olivia-world-mood>p{margin:0 0 0 32px}
      .olivia-world-emotion-detail{margin:0 0 8px 32px;font-size:14px}
      .olivia-world-emotion-detail>summary{color:#acb0b4;padding:4px 0}
      .olivia-world-emotion-entry{border-bottom:1px solid #383a3e;padding:14px 0}
      .olivia-world-emotion-entry h5{font-size:15px;margin:0}
      .olivia-world-emotion-entry p,.olivia-world-emotion-entry small{color:#acb0b4}
      .olivia-world-tabs{display:flex;gap:30px;border-bottom:1px solid #383a3e;margin-top:26px}
      [data-world-main] .olivia-world-tabs button{border:0;border-radius:0;padding:12px 0;font-size:15px;color:#acb0b4;border-bottom:2px solid transparent}
      [data-world-main] .olivia-world-tabs button[aria-selected=true]{border-bottom-color:#ded3bf;color:#eee6d9}
      .olivia-world-content{padding:26px 0;overflow-wrap:anywhere}
      [data-world-main] [hidden]{display:none!important}
      .olivia-world-agenda{padding:17px 0;border-bottom:1px solid #383a3e;font-size:15px}
      .olivia-world-agenda-detail>summary{display:list-item;list-style-position:inside;cursor:pointer}
      .olivia-world-agenda-detail>p{padding:0 12px 12px}
      [data-world-main] .olivia-world-status{font-size:12px;color:#acb0b4;border-top:1px solid #383a3e;padding-top:14px;margin-top:0}
      .olivia-world-basis{border-top:1px solid #383a3e;padding:14px 0;font-size:13px;color:#acb0b4}
      [data-world-main] .olivia-world-basis+.olivia-world-status{border:0;padding-top:0}
      [data-world-main] button:disabled{opacity:.5;cursor:wait}
      [data-world-main] button:hover:not(:disabled){background:#ffffff08}
      [data-world-main] button:focus-visible,[data-world-main] summary:focus-visible{outline:2px solid #ded3bf;outline-offset:4px}
      @media(max-width:800px){.olivia-world-columns{gap:20px;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr)}.olivia-world-aside{padding-left:20px}[data-world-main]{padding:20px}[data-world-main] h3{font-size:23px}[data-world-main] h4{font-size:18px}}
      @media(max-width:580px){.olivia-world-columns{grid-template-columns:1fr}.olivia-world-aside{padding:20px 0 0;border-left:0;border-top:1px solid #383a3e}.olivia-world-tabs{gap:20px}.olivia-world-heading{align-items:flex-start}.olivia-world-heading .olivia-world-line{align-items:flex-start}[data-world-main]{padding:18px}.olivia-world-meta{gap:8px}}
    `;
    const header=document.createElement('header');header.className='olivia-world-header';header.append(text('h1','世界'));
    const panel=document.createElement('section');panel.dataset.worldMain='';
    page.replaceChildren(style,header,panel);
    panel.append(text('p','正在读取林离的生活……'));
    void requestJson(STATUS_PATH).then(payload=>{if(page.isConnected)return renderPrivateWorldPanel(panel,payload.capabilities?.private_world)}).catch(()=>{
      if(page.isConnected)panel.replaceChildren(text('p','近况暂时无法读取。'),button('重试',()=>mountWorldPage(page)));
    });
  };
  const installNativeWorldRoute = () => {
    const native=window.__oliviaNativeView;
    if(!native?.router || !native.h)return;
    if(!native.router.hasRoute('olivia-world'))native.router.addRoute({
      path:'/world',name:'olivia-world',component:{
        name:'OliviaWorldView',
        render(){return native.h('main',{class:'mx-full h-full'})},
        mounted(){mountWorldPage(this.$el)},
        beforeUnmount(){this.$el.querySelector('[data-world-main]')?._lifeRequest && (this.$el.querySelector('[data-world-main]')._lifeRequest=null)},
      },
    });
    if(window.location.hash==='#/collection?view=world')native.router.replace('/world');
  };
  const mountMainNavigation = () => {
    const route = window.location.hash.split("?")[0];
    const world=window.location.hash===WORLD_ROUTE;
    let nav = document.querySelector("[data-olivia-main-navigation]");
    if (route !== "#/studio" && route !== "#/collection" && route !== WORLD_ROUTE) {
      nav?.remove();
      return;
    }
    if (!nav) {
      nav = document.createElement("nav");
      nav.setAttribute("data-olivia-main-navigation", "");
      nav.setAttribute("aria-label", "主导航");
      Object.assign(nav.style, {
        position: "fixed", top: "60px", left: "120px", zIndex: "20",
        display: "flex", gap: "8px", WebkitAppRegion: "no-drag",
      });
      for (const [label, href] of [["信箱", "#/collection"], ["世界", WORLD_ROUTE], ["曲库", "#/studio"]]) {
        const link = text("a", label, "text-body-m");
        link.href = href;
        link.addEventListener('click',event=>{
          if(event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
          const router=window.__oliviaNativeView?.router;if(!router)return;
          event.preventDefault();void router.push(href.slice(1));
        });
        Object.assign(link.style, {
          display: "inline-flex", alignItems: "center", minHeight: "36px",
          padding: "0 16px", borderRadius: "18px", border: "1px solid #6b7280",
          textDecoration: "none", whiteSpace: "nowrap", pointerEvents: "auto",
          WebkitAppRegion: "no-drag",
        });
        nav.append(link);
      }
      document.body.append(nav);
    }
    nav.style.left='120px';
    for (const link of nav.querySelectorAll("a")) {
      const active = link.getAttribute("href") === (world?WORLD_ROUTE:route);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
      link.style.color = active ? "#111827" : "#d1d5db";
      link.style.background = active ? "#d9d2c8" : "#17181a";
    }
  };

  const finishInitialSetup = async (skipped) => {
    if (!skipped) {
      const memory = await requestCapability(MEM0_CAPABILITY_PATH);
      if (memory.state !== "ready") {
        throw Object.assign(new Error("请先导入记忆包，等待准备完成。"), {code: "SETUP_MEMORY_REQUIRED"});
      }
      const current = await requestJson(STATUS_PATH);
      if (capabilityState(current.capabilities?.memory) !== "available") {
        throw Object.assign(new Error("记忆包已安装，正在准备记忆服务。请稍候再点开始使用。"), {code: "LLM_SETUP_MEMORY_PREPARING"});
      }
    }
    await requestSetup(SETUP_COMPLETE_PATH, { skipped });
    window.location.hash = "#/collection";
  };

  const openDialog = (initialMode = false, initialPanel = "llm") => {
    document.querySelector(`[${DIALOG_ATTR}]`)?.remove();

    const backdrop = document.createElement("div");
    backdrop.setAttribute(DIALOG_ATTR, "");
    backdrop.style.position = "fixed";
    backdrop.style.inset = "0";
    backdrop.style.zIndex = "2147483000";
    backdrop.style.display = "grid";
    backdrop.style.placeItems = "center";
    backdrop.style.padding = "40px";
    backdrop.style.background = "rgba(0, 0, 0, 0.62)";
    backdrop.style.pointerEvents = "auto";
    backdrop.style.webkitAppRegion = "no-drag";

    const dialog = document.createElement("section");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-labelledby", "olivia-companion-dialog-title");
    dialog.style.width = "min(820px, calc(100vw - 80px))";
    dialog.style.maxHeight = "calc(100vh - 80px)";
    dialog.style.overflow = "auto";
    dialog.style.borderRadius = "16px";
    dialog.style.padding = "28px";
    dialog.style.backgroundColor = "#18191c";
    dialog.style.boxShadow = "0 24px 80px rgba(0, 0, 0, 0.45)";
    dialog.style.color = "#f9fafb";
    dialog.style.colorScheme = "dark";
    dialog.style.pointerEvents = "auto";
    dialog.style.webkitAppRegion = "no-drag";

    const theme = document.createElement("style");
    theme.textContent = `
      [${DIALOG_ATTR}] [role="dialog"] .text-text-title,
      [${DIALOG_ATTR}] [role="dialog"] .text-text-body {
        color: #f9fafb !important;
      }
      [${DIALOG_ATTR}] [role="dialog"] .text-text-secondary {
        color: #cbd5e1 !important;
      }
      [${DIALOG_ATTR}] [role="dialog"] button,
      [${DIALOG_ATTR}] [role="dialog"] select,
      [${DIALOG_ATTR}] [role="dialog"] input,
      [${DIALOG_ATTR}] [role="dialog"] textarea {
        color: #f9fafb !important;
        background-color: #111827 !important;
        border-color: #6b7280 !important;
        color-scheme: dark !important;
      }
      [${DIALOG_ATTR}] [role="dialog"] button,
      [${DIALOG_ATTR}] [role="dialog"] select,
      [${DIALOG_ATTR}] [role="dialog"] input,
      [${DIALOG_ATTR}] [role="dialog"] textarea {
        -webkit-app-region: no-drag !important;
        pointer-events: auto !important;
      }
      [${DIALOG_ATTR}] [role="dialog"] option {
        color: #f9fafb !important;
        background-color: #111827 !important;
      }
      [${DIALOG_ATTR}] [role="tab"][aria-selected="true"] {
        color: #ffffff !important;
        background-color: #374151 !important;
        border-color: #93c5fd !important;
      }
      [${DIALOG_ATTR}] [role="tab"][aria-selected="false"] {
        color: #cbd5e1 !important;
        background-color: #111827 !important;
        border-color: #6b7280 !important;
      }
    `;

    const header = document.createElement("div");
    header.style.display = "flex";
    header.style.alignItems = "center";
    header.style.justifyContent = "space-between";
    header.style.gap = "24px";

    const serviceMode = initialPanel === "relay";
    const heading = text("h2", serviceMode ? "账户" : initialMode ? "欢迎使用 Olivia" : "长期记忆", "text-text-title text-headline-m");
    heading.id = "olivia-companion-dialog-title";
    heading.style.margin = "0";
    const dismiss = () => {
      backdrop.remove();
      void refreshVideoReplySetting();
    };
    const close = button(initialMode ? "稍后设置" : "关闭", async () => {
      if (initialMode) {
        try {
          await finishInitialSetup(true);
        } catch (_error) {
          return;
        }
      }
      dismiss();
    });
    header.append(heading, close);

    if (serviceMode) {
      const content = document.createElement("div");
      content.style.marginTop = "24px";
      {
        dialog.setAttribute("data-olivia-relay-dialog", "");
        theme.textContent += `
          [data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] p { margin:0; }
          [data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] [hidden] { display:none !important; }
          [data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] h3 { margin:0; font-size:16px;line-height:24px; }
          [data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] button { border-radius:10px !important; min-height:40px; }
          [data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] input,[data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] select { border-radius:10px !important; }
          [data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] button:focus-visible { outline:2px solid #ddd2bd;outline-offset:3px; }
          [data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] button:hover:not(:disabled) { filter:brightness(1.2); }
          [data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] .olivia-primary-action { background:#ded3bd !important;color:#202126 !important;border-color:#ded3bd !important; }
          [data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] .olivia-account-key { display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px 16px;align-items:end;padding:20px;background:#23252a;border-radius:12px; }
          [data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] .olivia-account-key > p { grid-column:1/-1;font-size:12px;color:#b9bcc4; }
          [data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] .olivia-account-metrics { display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:20px;padding:20px 0;border-bottom:1px solid #383b42; }
          [data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] .olivia-metric-label { font-size:12px;color:#b9bcc4;margin-bottom:8px; }
          [data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] .olivia-metric-value { font-size:24px;line-height:32px;font-weight:600;font-variant-numeric:tabular-nums;overflow-wrap:anywhere;color:#f1ece2; }
          [data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] [aria-pressed] { background:transparent !important;border:0 !important;border-radius:0 !important;border-bottom:2px solid transparent !important;padding:10px 4px !important; }
          [data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] [aria-pressed="true"] { border-bottom-color:#ded3bd !important;color:#f1ece2 !important; }
          @media(max-width:700px) {
            [data-olivia-relay-models] .olivia-model-row { grid-template-columns:20px minmax(0,1fr) !important;gap:4px 12px !important; }
            [data-olivia-relay-models] .olivia-model-multipliers { grid-column:2; }
            [data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] .olivia-account-key { grid-template-columns:minmax(0,1fr); }
            [data-olivia-companion-settings-dialog] [data-olivia-relay-dialog] .olivia-account-metrics { grid-template-columns:repeat(2,minmax(0,1fr));gap:16px; }
          }
        `;
        dialog.style.height = "min(800px, calc(100vh - 80px))";
        dialog.style.boxSizing = "border-box";
        dialog.style.display = "flex";
        dialog.style.flexDirection = "column";
        dialog.style.overflow = "hidden";
        header.style.flexShrink = "0";
        content.style.cssText = "display:grid;grid-template-rows:minmax(0,1fr);min-height:0;flex:1;margin-top:24px";
        const viewport = document.createElement("div");
        viewport.style.cssText = "min-height:0;overflow-y:auto;overflow-x:hidden;scrollbar-gutter:stable;padding-right:12px";
        // One account page: key, balance and the unified statement for replies and
        // media. Connecting or replacing a key is folded underneath.
        const account = document.createElement("section");
        const connection = document.createElement("details");
        connection.className = "olivia-account-connection";
        connection.style.cssText = "margin-top:24px;padding-top:20px;border-top:1px solid #8884";
        const connectionBody = document.createElement("section");
        connectionBody.style.cssText = "display:grid;gap:14px;min-width:0;margin-top:16px";
        const connectionTitle = text("summary", "已有 Key？在这里导入或更换");
        connectionTitle.style.cssText = "cursor:pointer;color:#b9bcc4";
        connection.append(connectionTitle, connectionBody);
        viewport.append(account, connection);
        content.append(viewport);
        void renderLlmSetupPanel(connectionBody, false);
        mountRelayAccount(account);
      }
      dialog.append(header, content);
      backdrop.append(theme, dialog);
      const opener = document.activeElement;
      close.addEventListener("click", () => { opener?.focus(); void refreshAccountEntry(); });
      backdrop.addEventListener("keydown", event => {
        if (event.key === "Escape") { dismiss(); opener?.focus(); void refreshAccountEntry(); }
        if (event.key === "Tab") {
          const items = Array.from(dialog.querySelectorAll('button,input,select,textarea,a[href]')).filter(item => !item.disabled && !item.hidden && item.getClientRects().length);
          const first = items[0], last = items[items.length - 1];
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
        }
      });
      document.body.append(backdrop);
      close.focus();
      return;
    }

    const status = text(
      "p",
      "正在连接本机陪伴服务……",
      "text-text-secondary text-body-m font-regular"
    );
    status.setAttribute("aria-live", "polite");
    status.style.margin = "20px 0";

    const tabs = document.createElement("div");
    tabs.setAttribute("role", "tablist");
    tabs.style.display = "flex";
    tabs.style.gap = "12px";
    tabs.style.marginBottom = "18px";

    const panels = document.createElement("div");
    const panelNodes = {};
    const definitions = initialMode
      ? [
          { id: "capability", label: "1 · 导入记忆包", key: "capability" },
          { id: "llm", label: "2 · 连接回信服务", key: "llm" },
        ]
      : [
          { id: "memory", label: "长期记忆", key: "memory" },
        ];

    // A single panel needs no tab row.
    if (definitions.length === 1) tabs.hidden = true;
    const showPanel = (id) => {
      for (const tab of tabs.querySelectorAll('[role="tab"]')) {
        const active = tab.dataset.panelId === id;
        tab.setAttribute("aria-selected", active ? "true" : "false");
      }
      for (const panel of panels.querySelectorAll('[role="tabpanel"]')) {
        const active = panel.dataset.panelId === id;
        panel.hidden = !active;
        panel.style.display = active ? "grid" : "none";
      }
    };

    for (const definition of definitions) {
      const tab = button(definition.label, () => showPanel(definition.id));
      tab.setAttribute("role", "tab");
      tab.dataset.panelId = definition.id;
      tab.setAttribute("aria-controls", `olivia-companion-panel-${definition.id}`);
      tabs.append(tab);

      const panel = document.createElement("section");
      panel.id = `olivia-companion-panel-${definition.id}`;
      panel.dataset.panelId = definition.id;
      panel.dataset.oliviaCompanionPanel = definition.id;
      panel.setAttribute("role", "tabpanel");
      panel.style.padding = "18px";
      panel.style.borderRadius = "12px";
      panel.style.background = "#202228";
      panel.style.display = "grid";
      panel.style.gap = "14px";
      panel.append(
        text("h3", definition.label, "text-text-title text-title-m"),
        text("p", "正在读取……", "text-text-secondary text-body-m font-regular")
      );
      panelNodes[definition.key] = panel;
      panels.append(panel);
    }

    const localVersion = text("p", "本地补丁版本：正在读取……", "text-text-secondary text-body-m font-regular");
    requestJson("/toy/updates/local/status").then((value) => {
      localVersion.textContent = typeof value.version === "string"
        ? `本地补丁版本：${value.version}（当前运行）`
        : "本地补丁版本：基础安装版";
    }).catch(() => { localVersion.textContent = "本地补丁版本：暂时无法读取"; });
    dialog.append(header, localVersion, status, tabs, panels);
    if (!initialMode) {
      dialog.setAttribute("data-olivia-memory-dialog", "");
      Object.assign(dialog.style, {width: "min(1160px, calc(100vw - 40px))",
        height: "calc(100vh - 48px)", maxHeight: "calc(100vh - 48px)",
        display: "flex", flexDirection: "column", overflow: "hidden", padding: "24px",
        boxSizing: "border-box"});
      backdrop.style.padding = "20px";
      header.style.flexShrink = "0";
      localVersion.style.cssText = "font-size:12px;color:#b3b5b8;margin:8px 0 12px;flex-shrink:0";
      status.style.cssText = "font-size:12px;margin:0 0 12px;flex-shrink:0";
      status.setAttribute("data-memory-dialog-status", "");
      panels.style.cssText = "flex:1;min-height:0;display:flex;flex-direction:column";
      panelNodes.memory.style.cssText = "flex:1;min-height:0;padding:0;display:grid;grid-template-rows:minmax(0,1fr);background:transparent";
      theme.textContent += `
        [data-olivia-memory-dialog] [data-memory-dialog-status][data-state="available"]{display:none!important}
        [data-olivia-memory-dialog] [hidden]{display:none!important}
        [data-olivia-memory-dialog]>div:first-child button{background:#1a1b1d!important;color:#e8e3db!important;border-color:#4c5055!important}
        @media(max-width:700px){
          [data-olivia-companion-settings-dialog]:has([data-olivia-memory-dialog]){padding:10px!important}
          [data-olivia-memory-dialog]{width:calc(100vw - 20px)!important;padding:16px!important}
        }
      `;
    }
    if (initialMode) {
      const finishActions = actions();
      finishActions.style.marginTop = "18px";
      const finish = button("开始使用", async () => {
        setButtonsBusy([finish], true);
        setDiagnosticDetails(status, []);
        try {
          await finishInitialSetup(false);
          backdrop.remove();
        } catch (_error) {
          const code = _error && _error.code;
          setDiagnosticDetails(status, code || "SETUP_CONNECTION_UNAVAILABLE");
          if (["SETUP_MEMORY_REQUIRED", "LLM_SETUP_MEMORY_PREPARING"].includes(code)) {
            status.textContent = _error.message;
            showPanel("capability");
          } else if (code === "LLM_SETUP_KEY_REQUIRED") {
            status.textContent = "请填写回信服务 Key，测试连接并保存，然后开始使用。";
            showPanel("llm");
          } else {
            status.textContent = "还未确认准备完成。请检查服务连接后重试，你已保存的设置会保留。";
          }
          setButtonsBusy([finish], false);
        }
      });
      finishActions.append(finish);
      dialog.append(finishActions);
    }
    backdrop.append(theme, dialog);
    backdrop.addEventListener("click", (event) => {
      // Import progress must not disappear because of an accidental outside click.
      if (event.target === backdrop) event.preventDefault();
    });
    backdrop.addEventListener("keydown", (event) => {
      if (!initialMode && event.key === "Escape") {
        dismiss();
      }
    });
    document.body.append(backdrop);
    showPanel(initialMode ? "capability" : definitions.some(item => item.id === initialPanel) ? initialPanel : definitions[0].id);
    close.focus();
    loadDialogData(status, panelNodes, initialMode);
  };

  const findSettingsContainer = () => {
    for (const main of document.querySelectorAll("main")) {
      const sections = main.querySelectorAll(".tp-settings-item");
      if (sections.length) {
        return sections[sections.length - 1].parentElement;
      }
    }
    return null;
  };

  const REPLY_ROUTE_LABELS = {
    voice_reply: "说话",
    singing_video: "唱歌",
    voice_song_video: "说话＋唱歌",
  };
  const routeRequest = async (path, body) => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 330000);
    try {
      const response = await fetch(new URL(path, apiBase), {
        method: body ? "POST" : "GET", cache: "no-store", credentials: "omit",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        ...(body ? { body: JSON.stringify(body) } : {}), signal: controller.signal,
      });
      const payload = await response.json();
      if (!response.ok || payload.code !== 0) throw new Error(payload.data?.error_code || "设置读取失败，请重试");
      return payload.data;
    } finally { window.clearTimeout(timeout); }
  };
  const confirmReplyRoute = (route, ready, video = false, readiness = {}) => new Promise((resolve) => {
    const cloudUnavailable = !ready && readiness.backend === 'remote';
    const cloudMessages = {
      GPU_NOT_CONFIGURED: '请先连接 Olivia 账户。',
      GPU_TLS_FAILED: '无法验证云端证书，请检查系统时间和网络。',
      GPU_CONNECTION_TIMEOUT: '云端检查超时，请稍后重试。',
      GPU_CONNECT_FAILED: '无法连接云端服务，请检查网络后重试。',
      GPU_CONNECTION_FAILED: '云端连接中断，请重试。',
      GPU_AUTH_FAILED: '云端认证失败，请检查 Olivia 账户。',
      GPU_RESPONSE_INVALID: '云端返回异常，请重试。',
      GPU_QUEUE_FULL: '云端繁忙，请稍后重试。',
      GPU_CAPABILITY_UNAVAILABLE: '云端当前未提供所需生成能力，请稍后重试。',
    };
    const dialog = document.createElement("dialog");
    dialog.setAttribute("aria-label", "确认回信形式");
    dialog.setAttribute("data-olivia-route-confirm", "");
    dialog.style.cssText = "position:fixed;inset:0;margin:auto;background:#191a1c;color:#ded9d1;border:1px solid #66696f;border-radius:16px;padding:28px;max-width:480px;max-height:calc(100% - 48px);overflow:auto;width:calc(100% - 48px);box-sizing:border-box;font-family:inherit;";
    const title = text("h3", ready ? "本次开启回信形式？" : cloudUnavailable ? "云端生成暂不可用" : "需要准备回信组件", "text-title-m");
    title.style.marginBottom = "12px";
    const explanation = text("p", ready
      ? `这封信请求了${REPLY_ROUTE_LABELS[route]}${video ? "视频" : ""}，但你已关闭该形式。可以仅为这封信开启，长期设置保持不变。`
      : cloudUnavailable
        ? `${cloudMessages[readiness.error_code] || '云端能力检查失败，请稍后重试。'}（${Object.hasOwn(cloudMessages, readiness.error_code) ? readiness.error_code : 'GPU_REQUEST_FAILED'}）信件尚未寄出，草稿已保留。`
        : `这封信请求了${REPLY_ROUTE_LABELS[route]}，当前缺少所需组件。请先在本地组件中准备好，再发送。`, "text-body-m font-regular");
    explanation.style.cssText = "margin-bottom:20px;line-height:1.7;";
    const shade = document.createElement("style");
    shade.textContent = "dialog[data-olivia-route-confirm]::backdrop{background:rgba(0,0,0,.6)}";
    dialog.append(shade, title, explanation);
    const previous = document.activeElement;
    const finish = (accepted) => { dialog.close(); dialog.remove(); previous?.focus(); resolve(accepted); };
    const controls = actions();
    controls.append(button("返回修改", () => finish(false)));
    if (cloudUnavailable) controls.append(button("重新检查", () => finish('retry')));
    if (ready) controls.append(button("仅本次开启并发送", () => finish(true)));
    dialog.append(controls);
    dialog.addEventListener("cancel", (event) => { event.preventDefault(); finish(false); });
    document.body.append(dialog); dialog.showModal();
  });
  const composerCovers = new WeakMap();
  let coverComposer = null;
  const mountCoverComposer = (input) => {
    // The native content element is the anchor, never the full-screen overlay.
    const paper=input.closest('.mail-box-write-dialog-content');
    const owner=paper?.closest('.mail-box-write-dialog');
    if(!paper||!owner)return;
    coverComposer=input;
    if(composerCovers.has(input))return;
    if(!document.getElementById('olivia-composer-style')){
      const style=document.createElement('style');style.id='olivia-composer-style';
      style.textContent=`
        .olivia-compose-grid{--rail:108px;--gap:16px;display:grid;grid-template-columns:calc(100% - var(--rail) - var(--gap)) var(--rail);gap:var(--gap);width:100%;min-width:0;transition:grid-template-columns .32s cubic-bezier(.16,1,.3,1)}
        .olivia-compose-grid[data-mode=cover]{grid-template-columns:var(--rail) calc(100% - var(--rail) - var(--gap))}
        .olivia-compose-pane{min-width:0;overflow:hidden}
        .olivia-compose-body{width:var(--body-width)!important;opacity:1;visibility:visible;transition:opacity .18s ease-out,visibility 0s}
        .olivia-compose-grid .olivia-compose-body[hidden]{display:block!important;opacity:0;visibility:hidden;pointer-events:none;transition:opacity .12s ease-out,visibility 0s .12s}
        .olivia-compose-grid .olivia-compose-cover[hidden]{display:flex!important}
        .olivia-compose-pane>button{margin:0 0 16px;max-width:100%;padding:10px 20px;white-space:nowrap}
        .olivia-compose-grid button{border:1px solid #686a70;border-radius:999px;color:#ded9d1;background:transparent;min-height:40px;font:inherit;cursor:pointer}
        .olivia-compose-grid button[aria-expanded=true],.olivia-compose-grid button[aria-pressed=true]{background:#ded9d1;color:#202124;border-color:#ded9d1}
        .olivia-compose-grid button:focus-visible,.olivia-compose-grid textarea:focus-visible{outline:2px solid #ded9d1;outline-offset:3px}
        .olivia-compose-grid button:disabled{opacity:.5;cursor:default}
        .olivia-compose-grid [hidden]{display:none!important}
        .olivia-compose-cover{display:flex;flex-direction:column;gap:16px;color:#ded9d1;min-height:360px}
        .olivia-compose-cover p{margin:0;line-height:1.6;color:#b8b9bf}
        .olivia-compose-cover .olivia-cover-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
        .olivia-compose-cover .olivia-cover-name{flex:1;min-width:80px;overflow-wrap:anywhere}
        .olivia-compose-cover textarea{width:100%;min-height:170px;resize:vertical;box-sizing:border-box;padding:16px;background:#191a1c;color:#ded9d1;border:1px solid #686a70;border-radius:12px;font:inherit;line-height:1.7}
        .olivia-compose-cover textarea::placeholder{color:#b8b9bf}
        .olivia-compose-cover .olivia-cover-wave{position:relative;flex:1;min-width:100px;height:60px}
        .olivia-cover-wave canvas{width:100%;height:60px;filter:brightness(3)}
        .olivia-cover-wave input{position:absolute;inset:0;width:100%;height:60px;opacity:0;cursor:pointer}
        .olivia-cover-wave:focus-within{outline:2px solid #ded9d1;outline-offset:2px}
        .olivia-cover-play{width:44px;padding:0!important;font-size:18px!important}
        .olivia-cover-time{font-size:12px;font-variant-numeric:tabular-nums;color:#b8b9bf}
        .olivia-cover-options{display:flex;gap:8px;flex-wrap:wrap}
        .olivia-compose-hint{margin:16px 0 0;color:#b8b9bf;font-size:13px}
        .olivia-compose-grid,.olivia-compose-grid[data-mode=cover]{display:grid;grid-template-columns:minmax(0,1fr);grid-template-rows:auto minmax(0,1fr);height:clamp(340px,56vh,560px);gap:20px;transition:none;color-scheme:dark}
        .olivia-compose-navigation{display:flex;gap:12px;grid-row:1}
        .olivia-compose-navigation button{padding:10px 22px}
        .olivia-compose-grid>.olivia-compose-body,.olivia-compose-music{grid-row:2;grid-column:1;min-height:0;overflow:auto;width:100%!important;box-sizing:border-box}
        .olivia-compose-grid>.olivia-compose-body{overflow:hidden}
        .olivia-compose-grid .mail-box-write-dialog-content{width:100%;height:100%;min-width:0;min-height:0;margin:0;aspect-ratio:auto;box-sizing:border-box;padding:16px 142px 16px 16px;overflow:hidden}
        .olivia-compose-grid .mail-box-write-dialog-content-textarea{display:block;width:100%;height:100%;min-width:0;min-height:0;margin:0;box-sizing:border-box;overflow-x:hidden;overflow-y:auto;overflow-wrap:anywhere}
        .olivia-compose-music{display:grid;grid-template-rows:auto minmax(0,1fr);gap:20px;overflow:hidden}
        .olivia-music-tabs{display:flex;gap:24px;border-bottom:1px solid #45464b}
        .olivia-music-tabs button{padding:0 0 12px;border:0!important;border-radius:0;background:transparent!important;color:#b8b9bf;min-height:38px}
        .olivia-music-tabs button[aria-pressed=true]{color:#f0eade;box-shadow:inset 0 -2px #ded9d1}
        .olivia-compose-original,.olivia-compose-music>.olivia-compose-cover{grid-row:2;grid-column:1;overflow:auto;min-height:0;padding:0 12px 12px 0;box-sizing:border-box;width:100%!important;scrollbar-gutter:stable}
        .olivia-compose-original{display:flex;flex-direction:column;gap:24px}
        .olivia-original-intent{display:flex;flex-direction:column;gap:10px}
        .olivia-original-intent textarea{width:100%;min-height:100px;resize:vertical;padding:14px 16px;border:1px solid #686a70;border-radius:12px;background:#191a1c;color:#ded9d1;box-sizing:border-box;font:inherit;line-height:1.6}
        .olivia-original-intent small{color:#b8b9bf;font-size:13px}
        .olivia-compose-original [data-olivia-music-settings]{border:0!important;padding:0!important}
        .olivia-compose-original [data-olivia-music-settings] input:not([type=checkbox]),.olivia-compose-original [data-olivia-music-settings] textarea,.olivia-compose-original [data-olivia-music-settings] select{background:#191a1c!important;border-color:#686a70!important;color:#ded9d1!important}
        .olivia-compose-original [data-olivia-music-settings] textarea{min-height:82px}
        .olivia-compose-original [data-olivia-music-settings] summary{padding:12px 0;cursor:pointer;border-top:1px solid #45464b}
        .olivia-compose-original [data-olivia-music-settings] button{padding:8px 16px}
        .olivia-compose-original .olivia-cover-options{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
        .olivia-compose-original .olivia-cover-options button{padding:8px 16px}
        .olivia-compose-grid button:hover:not(:disabled){border-color:#ded9d1}
        .olivia-compose-grid .olivia-compose-body{transition:none!important}
        .olivia-compose-grid .olivia-compose-body[hidden]{visibility:hidden!important;opacity:0!important;transition:none!important}
        .olivia-compose-original [data-olivia-music-settings] label>span>span{display:block;color:#b8b9bf;font-size:13px;line-height:1.6;margin-top:3px}
        .olivia-compose-original [data-olivia-music-settings] label>small{color:#b8b9bf;font-size:13px}
        .olivia-compose-original [data-olivia-music-settings] form{gap:18px!important}
        .olivia-compose-navigation button,.olivia-music-tabs button{transition:background-color .2s ease,color .2s ease,border-color .2s ease,box-shadow .2s ease}
        @keyframes olivia-compose-enter{from{opacity:.35;transform:translateY(7px)}to{opacity:1;transform:translateY(0)}}
        .olivia-compose-grid>.olivia-compose-body:not([hidden]),.olivia-compose-music:not([hidden]),.olivia-compose-original:not([hidden]),.olivia-compose-music>.olivia-compose-cover:not([hidden]){animation:olivia-compose-enter .24s cubic-bezier(.16,1,.3,1) both}
        @media(prefers-reduced-motion:reduce){.olivia-compose-grid *{animation:none!important;transition:none!important}}
        @media(max-width:700px){.olivia-compose-grid{--rail:84px;--gap:8px}.olivia-compose-pane>button{font-size:12px;padding:8px 4px}.olivia-compose-cover{gap:12px}.olivia-compose-cover textarea{padding:12px}}
        @media(prefers-reduced-motion:reduce){.olivia-compose-grid,.olivia-compose-grid .olivia-compose-body{transition:none!important}}
      `;document.head.append(style);
    }
    const state={mode:'letter',musicMode:'original',draft:input.value,material:null,busy:false,output:'audio'};
    composerCovers.set(input,state);
    const frame=paper.parentElement;
    const grid=document.createElement('div');grid.className='olivia-compose-grid';grid.dataset.mode='letter';
    const left=document.createElement('section'),right=document.createElement('section');
    left.className=right.className='olivia-compose-pane';
    const cover=document.createElement('div');cover.className='olivia-compose-cover olivia-compose-body';cover.dataset.oliviaCoverPanel='';frame.classList.add('olivia-compose-body');
    const setValue=value=>{input.value=value;input.dispatchEvent(new Event('input',{bubbles:true}))};
    const syncCoverText=()=>{if(state.mode==='cover')setValue(state.material ? `请翻唱《${state.material.filename.replace(/\.[^.]+$/,'')}》。` : '')};
    const select=mode=>{
      if(mode===state.mode)return;
      if(state.mode==='letter')state.draft=input.value;
      state.mode=mode;if(mode!=='letter')state.musicMode=mode;grid.dataset.mode=mode;
      frame.hidden=mode!=='letter';frame.inert=mode!=='letter';music.hidden=mode==='letter';music.inert=mode==='letter';
      cover.hidden=mode!=='cover';cover.inert=mode!=='cover';original.hidden=mode!=='original';original.inert=mode!=='original';
      normal.setAttribute('aria-expanded',String(mode==='letter'));sing.setAttribute('aria-expanded',String(mode!=='letter'));
      originalTab.setAttribute('aria-pressed',String(mode==='original'));coverTab.setAttribute('aria-pressed',String(mode==='cover'));
      if(mode==='letter'){audio.pause();setValue(state.draft)}else if(mode==='original'){audio.pause();syncOriginalText()}else syncCoverText();
    };
    state.select=select;
    const normal=button('普通信件',()=>select('letter')),sing=button('演唱与翻唱',()=>select(state.musicMode));
    normal.setAttribute('aria-expanded','true');sing.setAttribute('aria-expanded','false');
    const music=document.createElement('div');music.className='olivia-compose-music';music.hidden=true;music.inert=true;
    const tabs=document.createElement('div');tabs.className='olivia-music-tabs';tabs.setAttribute('aria-label','歌曲类型');
    const originalTab=button('原创演唱',()=>select('original')),coverTab=button('歌曲翻唱',()=>select('cover'));tabs.append(originalTab,coverTab);
    const original=document.createElement('div');original.className='olivia-compose-original';original.hidden=true;
    const intentLabel=document.createElement('label');intentLabel.className='olivia-original-intent';
    const intent=document.createElement('textarea');intent.maxLength=8000;intent.rows=3;intent.placeholder='想听什么主题？写下故事、心情，或想对林离说的话…';
    intentLabel.append(text('strong','这次想听她唱什么？'),intent,text('small','林离会根据你的想法写词并演唱，完成后送到信箱。'));
    const syncOriginalText=()=>{if(state.mode==='original')setValue('请为我原创演唱一首歌曲。'+intent.value.trim())};intent.addEventListener('input',syncOriginalText);
    original.append(intentLabel);const musicControls=mountMusicSettings(original,true);
    const navigation=document.createElement('div');navigation.className='olivia-compose-navigation';navigation.append(normal,sing);
    frame.before(grid);music.append(tabs,original,cover);grid.append(navigation,frame,music);cover.hidden=true;cover.inert=true;
    // Keep full-width content mounted so switching cannot reflow the paper or resize the dialog.
    const sizeBodies=()=>{grid.style.setProperty('--body-width',grid.clientWidth+'px')};
    sizeBodies();const bodyResize=new ResizeObserver(sizeBodies);bodyResize.observe(grid);
    const status=text('p','选择原曲后自动识别歌词，你可以修改后再寄出。');status.setAttribute('role','status');
    const file=document.createElement('input');file.type='file';file.accept='.wav,.flac,.mp3,.m4a,.ogg';file.hidden=true;
    // File-picker cancellation bubbles as "cancel"; it must not close the letter dialog.
    file.addEventListener('cancel',event=>event.stopPropagation());
    file.addEventListener('click',event=>event.stopPropagation());
    const filename=text('span','未选择原曲');filename.className='olivia-cover-name';
    const choose=button('选择原曲',()=>file.click());
    const remove=button('移除',()=>{if(state.busy)return;audio.pause();state.material=null;lyrics.value='';file.value='';filename.textContent='未选择原曲';choose.textContent='选择原曲';player.hidden=true;syncCoverText();update()});
    const sourceRow=document.createElement('div');sourceRow.className='olivia-cover-row';sourceRow.append(choose,filename,remove,file);
    const audio=new Audio();audio.preload='metadata';let objectUrl=null;
    const player=document.createElement('div');player.className='olivia-cover-row';player.hidden=true;
    const play=button('▶',async()=>{if(audio.paused){waveCleanup?.start();try{await audio.play()}catch{status.textContent='原曲暂时无法播放，请重新选择文件。'}}else audio.pause()});play.className+=' olivia-cover-play';play.setAttribute('aria-label','播放原曲');
    const wave=document.createElement('div');wave.className='olivia-cover-wave';
    const seek=document.createElement('input');seek.type='range';seek.min=0;seek.max=0;seek.step='.1';seek.value=0;seek.setAttribute('aria-label','原曲播放进度');seek.oninput=()=>{audio.currentTime=Number(seek.value)};
    const time=text('span','0:00 / 0:00');time.className='olivia-cover-time';
    const clock=n=>Number.isFinite(n)?`${Math.floor(n/60)}:${String(Math.floor(n%60)).padStart(2,'0')}`:'0:00';
    const playback=()=>{play.textContent=audio.paused?'▶':'Ⅱ';play.setAttribute('aria-label',audio.paused?'播放原曲':'暂停原曲');seek.max=Number.isFinite(audio.duration)?audio.duration:0;seek.value=audio.currentTime;time.textContent=`${clock(audio.currentTime)} / ${clock(audio.duration)}`};
    ['play','pause','ended','loadedmetadata','timeupdate'].forEach(name=>audio.addEventListener(name,playback));
    player.append(play,wave,time);const waveCleanup=window.__oliviaLetterWave?.(wave,seek,audio,'');
    const lyrics=document.createElement('textarea');lyrics.maxLength=30000;lyrics.setAttribute('aria-label','翻唱歌词');lyrics.placeholder='选择原曲后自动识别，也可手动填写歌词。';
    const transcribe=async()=>{
      if(state.busy||!state.material)return;
      state.busy=true;update();status.textContent='正在本地识别歌词，可以切回普通信件继续写信。';
      const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),660000);
      try{
        const response=await fetch(new URL('/toy/cover/lyrics',apiBase),{method:'POST',credentials:'omit',signal:controller.signal,
          headers:{'Content-Type':'application/json',[CONFIRM_HEADER]:CONFIRM_VALUE},body:JSON.stringify({source_id:state.material.cover_source_id})});
        const result=await response.json();if(!response.ok||result.code!==0)throw Error('asr');
        if(result.data.deferred){state.lyricsOnServer=true;status.textContent='云端将在生成时识别歌词，也可手动填写。';return}
        lyrics.value=result.data.lyrics;state.material.cover_language=result.data.language||'unknown';status.textContent='歌词已自动识别，请核对并修正。';
      }catch{status.textContent='未能识别歌词。原曲已保留，可手动填写或重新识别。'}
      finally{clearTimeout(timer);state.busy=false;update()}
    };
    const recognize=button('重新识别',()=>void transcribe());
    const labelRow=document.createElement('div');labelRow.className='olivia-cover-row';labelRow.append(text('span','歌词'),recognize);
    const options=document.createElement('div');options.className='olivia-cover-options';
    const coverParameters=document.createElement('details');coverParameters.append(text('summary','翻唱参数'));
    const coverFields={};
    for(const [name,title,value,help] of [
      ['audio_cover_strength','原曲保留强度',0.6,'控制对原曲的保留程度，推荐 0.6。'],
      ['cover_noise_strength','翻唱噪声',0.25,'控制生成时加入的噪声强度，推荐 0.25；不是音量或降噪。']]){
      const row=document.createElement('label');row.style.cssText='display:grid;grid-template-columns:1fr 90px;gap:8px;margin:12px 0';
      const field=document.createElement('input');field.type='number';field.min='0';field.max='1';field.step='0.01';field.value=String(value);field.required=true;field.name=name;
      field.style.cssText='width:100%;padding:8px;border:1px solid #8886;border-radius:8px;background:transparent;color:inherit';
      field.setAttribute('aria-label',title);const note=text('small',help);note.style.gridColumn='1 / -1';
      row.append(text('span',title),field,note);coverParameters.append(row);coverFields[name]=field;
    }
    coverParameters.append(button('恢复推荐参数',()=>{coverFields.audio_cover_strength.value='0.6';coverFields.cover_noise_strength.value='0.25'}));
    for(const [mode,label] of [['audio','音频回信'],['video','视频回信']]){const item=button(label,()=>{state.output=mode;for(const node of options.children)node.setAttribute('aria-pressed',String(node===item))});item.setAttribute('aria-pressed',String(mode==='audio'));options.append(item)}
    const update=()=>{choose.disabled=remove.disabled=lyrics.disabled=state.busy;remove.hidden=!state.material;recognize.hidden=state.lyricsOnServer===true;recognize.disabled=state.busy||!state.material};
    state.materialForSend=()=>{
      if(state.mode==='original')return {original_output:state.originalOutput||'audio',music_options:musicControls.read()};
      if(state.busy)throw Error('原曲还在准备中，请稍候再寄出。');
      if(!state.material)throw Error('请先选择原曲。');
      if(!lyrics.value.trim()&&!state.lyricsOnServer){lyrics.focus();throw Error('请填写或识别歌词后再寄出。')}
      const cover_options={};for(const [name,field] of Object.entries(coverFields)){if(!field.reportValidity())throw Error('翻唱参数请填写 0 到 1 之间的数值。');cover_options[name]=Number(field.value);}
      return {...state.material,cover_lyrics:lyrics.value,cover_output:state.output,cover_options};
    };
    file.onchange=async()=>{
      const selected=file.files[0];if(!selected)return;
      if(selected.size>256*1024*1024){status.textContent='请选择不超过 256 MiB 的音频。';file.value='';return}
      state.busy=true;update();status.textContent='正在上传并检查原曲…';
      try{
        const response=await fetch(new URL('/toy/cover/upload',apiBase),{method:'POST',credentials:'omit',headers:{'Content-Type':'application/octet-stream',[CONFIRM_HEADER]:CONFIRM_VALUE},body:selected});
        const result=await response.json();if(!response.ok||result.code!==0)throw Error('upload');
        audio.pause();if(objectUrl)URL.revokeObjectURL(objectUrl);objectUrl=URL.createObjectURL(selected);audio.src=objectUrl;player.hidden=false;
        state.material={cover_source_id:result.data.source_id,filename:selected.name,cover_language:'unknown'};lyrics.value='';filename.textContent=selected.name;choose.textContent='更换原曲';syncCoverText();
        state.busy=false;
        state.lyricsOnServer=result.data.lyrics_on_server===true;
        lyrics.placeholder=state.lyricsOnServer?'可留空，由云端生成时识别；也可手动填写歌词。':'选择原曲后自动识别，也可手动填写歌词。';
        if(state.lyricsOnServer)status.textContent='原曲已准备好。歌词可留空，由云端生成时识别。';
        else if(result.data.asr_available)await transcribe();else status.textContent='原曲已上传。请导入歌词识别组件，或手动填写歌词。';
      }catch{status.textContent='上传失败，请检查音频文件后重新选择。之前的草稿和原曲仍保留。'}
      finally{state.busy=false;file.value='';update()}
    };
    cover.append(status,sourceRow,player,labelRow,lyrics,coverParameters,options,text('p','翻唱完成后会出现在信箱中，也可收藏到曲库。'));
    const originalOutput=document.createElement('div');originalOutput.className='olivia-cover-options';
    originalOutput.append(text('span','回信形式'));
    for(const [value,label] of [['audio','音频回信'],['video','视频回信']]){const item=button(label,()=>{state.originalOutput=value;for(const node of originalOutput.querySelectorAll('button'))node.setAttribute('aria-pressed',String(node===item))});item.setAttribute('aria-pressed',String(value==='audio'));originalOutput.append(item)}
    original.append(originalOutput);
    const hint=text('p','切换保留内容 · 寄出当前展开的内容');hint.className='olivia-compose-hint';grid.after(hint);update();
    const shell=owner.closest('[role="dialog"],.el-dialog')||owner.parentElement;
    const clear=Array.from(shell.querySelectorAll('button')).find(node=>node.textContent.trim()==='清空');
    clear?.addEventListener('click',event=>{
      if(state.mode==='letter')return;
      event.preventDefault();event.stopImmediatePropagation();
      if(state.mode==='original'){intent.value='';syncOriginalText();return}
      if(state.busy){status.textContent='原曲仍在准备中，请完成后再清空。';return}
      remove.click();status.textContent='翻唱内容已清空，普通信件草稿保留。';
    },true);
    // Release only this editor's media when the native composer is removed.
    const dispose=new MutationObserver(()=>{if(grid.isConnected)return;audio.pause();waveCleanup?.();bodyResize.disconnect();if(objectUrl)URL.revokeObjectURL(objectUrl);dispose.disconnect()});dispose.observe(document.body,{childList:true,subtree:true});
  };
  window.__oliviaPrepareLetterRoute = async (config) => {
    const endpoint = new URL(config.url, config.baseURL || apiBase);
    if (endpoint.origin !== new URL(apiBase).origin || !/^\/(?:toy\/)?letter\/(?:send|resend)$/.test(endpoint.pathname)) return config;
    const resending = endpoint.pathname.endsWith('/resend');
    if (proactiveState.busy) {
      const error = new Error("林离正在写信，完成后就可以寄出。草稿会保留。");
      error.code = "PROACTIVE_LETTER_BUSY";
      error.config = config;
      throw error;
    }
    const body = typeof config.data === "string" ? JSON.parse(config.data) : config.data;
    if (!body || (!resending && (typeof body.content !== "string" || !body.content.trim()))) return config;
    let preview;
    const editor=!resending && coverComposer?.isConnected && coverComposer.value===body.content ? composerCovers.get(coverComposer) : null;
    let attachment=null;
    try{if(editor && editor.mode!=='letter')attachment=editor.materialForSend()}
    catch(error){error.config=config;throw error}
    try { do { preview = await routeRequest("/toy/letter/route-preview", {...(resending ? {letter_id:body.letter_id || body.letterId} : {content: body.content}),
      ...(attachment?.original_output ? {original_output:attachment.original_output,music_options:attachment.music_options} :
        attachment ? {cover_source_id:attachment.cover_source_id,cover_output:attachment.cover_output} : {})});
      if (!preview.requested_route || preview.ready || preview.readiness?.backend !== 'remote') break;
      if (await confirmReplyRoute(preview.requested_route, false, preview.needs_video_confirmation, preview.readiness) !== 'retry') {
        throw Object.assign(new Error('已取消发送，信件内容保留'), {name:'CanceledError',code:'ERR_CANCELED',__CANCEL__:true});
      }
    } while (true); }
    catch (error) {
      error.config = config;
      if (error.__CANCEL__) throw error;
      if (!/^[A-Z][A-Z0-9_]{0,95}$/.test(error.message || "")) {
        const clientCode = error.name === "AbortError" ? "REPLY_ROUTE_CLIENT_TIMEOUT"
          : error instanceof TypeError ? "REPLY_ROUTE_CLIENT_CONNECTION"
          : error instanceof SyntaxError ? "REPLY_ROUTE_CLIENT_RESPONSE_INVALID" : "REPLY_ROUTE_CLIENT_UNKNOWN";
        // Best effort only: a disconnected backend cannot accept diagnostics.
        void fetch(new URL("/toy/letter/route-preview-diagnostic", apiBase), {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({error_code: clientCode}),
        }).catch(() => {});
        error.message = clientCode;
      }
      const messages = {OLIVIA_KEY_REQUIRED:"还没有连接 Olivia 账户 Key，请先在设置的“回信服务 → Olivia 账户”获取或导入 Key，再寄信。",REPLY_SERVICE_NOT_CONNECTED:"回信服务还没有连上你的 Olivia 账户 Key，请在设置的“回信服务 → Olivia 账户”点“连接并保存”，再寄信。",
        LLM_QUOTA_EXHAUSTED:"Olivia 余额不足，请在“回信服务 → Olivia 账户”充值后再寄信。",
        REPLY_ROUTE_CLIENT_TIMEOUT:"回信形式检测超时，请稍后重试。",
        REPLY_ROUTE_CLIENT_CONNECTION:"无法连接回信形式检测服务，请检查本地服务是否运行。",
        REPLY_ROUTE_CLIENT_RESPONSE_INVALID:"本地回信形式检测接口返回格式异常，请导出诊断包。",
        REPLY_ROUTE_CLIENT_UNKNOWN:"前端回信形式检测失败，请导出诊断包。",
        LLM_AUTH_FAILED:"大模型服务认证失败，请检查 API Key 和访问权限。",
        LLM_RATE_LIMITED:"大模型服务请求过于频繁，请稍后重试。",
        LLM_TIMEOUT:"大模型服务响应超时，请稍后重试。",
        LLM_TLS_FAILED:"无法验证大模型服务的 HTTPS 证书。请先同步 Windows 日期、时间和时区；确认使用最新版 Olivia。若使用代理或 HTTPS 检查软件，请检查其证书配置，或换一个可信网络重试。仍失败请导出诊断包；不要关闭证书校验。",
        LLM_DNS_FAILED:"无法解析大模型服务地址。请检查服务地址拼写、网络和 DNS 设置，恢复后重试；无需重新导入模型。",
        LLM_CONNECTION_FAILED:"与大模型服务的连接失败或中断。请检查网络、代理及防火墙是否允许 Olivia 访问服务地址，稍后重试；持续失败请导出诊断包。",
        LLM_SERVICE_UNAVAILABLE:"大模型服务暂时不可用，请稍后重试。持续失败请将诊断包交给服务提供方排查；无需重装本地组件。",
        LLM_USAGE_PENDING:"这次模型请求中断，用量等待核对，请暂勿反复寄信并保留诊断包。",
        LLM_REQUEST_DUPLICATE:"这次请求已经提交，请先查看原信件状态。",
        REPLY_ROUTE_INVALID_RESULT:"大模型返回的回信形式无法识别，请重试；若持续出现，请导出诊断包。",
        MEM0_EMBEDDING_CACHE_UNAVAILABLE:"长期记忆模型尚未安装或校验未通过。请打开本地陪伴的长期记忆页面，安装或修复模型后重试。",
        MEM0_IMPORT_FAILED:"长期记忆运行组件缺失或无法加载。请在本地陪伴中修复长期记忆组件后重试。",
        REPLY_ROUTE_INVALID_CONTENT:"信件内容无法用于检测回信形式，请检查后重试。",
        VIDEO_TRIAGE_UNAVAILABLE:"回信形式检测服务暂不可用，请重试；若持续出现，请导出诊断包。",
        REPLY_ROUTE_PREVIEW_EXPIRED:"检测期间回信设置发生变化，请重新寄出。"};
      const code = /^[A-Z][A-Z0-9_]{0,95}$/.test(error.message || "") ? error.message : null;
      const fallback = error.name === "AbortError" ? "回信形式检测超时，请稍后重试。"
        : error instanceof TypeError ? "无法连接回信形式检测服务，请检查本地服务是否运行。"
        : "回信形式检测失败，请导出诊断包。";
      const memoryImportFailure = /^MEM0_INIT_IMPORT_[A-Z_]+_(?:IMPORT|MODULE_MISSING)$/.test(code || "");
      error.message = (messages[code] || (memoryImportFailure
        ? "长期记忆运行组件加载失败，当前无法生成回信。请修复长期记忆组件后重试，信件草稿已保留。"
        : fallback)) + (code ? `（${code}）` : "") + "信件尚未寄出。";
      throw error;
    }
    let once;
    let videoOnce;
    if (preview.image_enabled && !await confirmAction(
      (preview.image_requested ? '本次回信预计生成图片附件。' : '图片附件已开启，本次回信可能生成图片。') +
      '生成图片会按图片服务计费。是否附图以正式回信判断为准，未生成不会收取图片费用。确认寄出？')) {
      throw Object.assign(new Error('已取消发送，草稿保留。'),{config,code:'ERR_CANCELED',__CANCEL__:true});
    }
    if (preview.requested_route && (!preview.ready || preview.needs_confirmation || preview.needs_video_confirmation)) {
      if (!await confirmReplyRoute(preview.requested_route, preview.ready, preview.needs_video_confirmation)) {
        const error = new Error("已取消发送，信件内容保留"); error.name = "CanceledError"; error.code = "ERR_CANCELED"; error.__CANCEL__ = true; throw error;
      }
      if (preview.needs_confirmation) once = preview.requested_route;
      if (preview.needs_video_confirmation) videoOnce = preview.requested_route;
    }
    const material = {...(body.material || {}), ...(attachment || {}), route_preview_token: preview.token};
    delete material.filename;
    if (!resending && preview.requires_cover_audio && !material.cover_source_id) {
      editor?.select('cover');
      throw Object.assign(new Error("请在翻唱页选择原曲并确认歌词，再点击寄出。"),{config,code:"ERR_CANCELED",__CANCEL__:true});
    }
    delete material.route_allow_once;
    delete material.route_video_once;
    let quote={paid:false};
    const originalCharge=!preview.requires_cover_audio&&['singing_video','voice_song_video','musical_video'].includes(preview.reply_mode);
    try{if(preview.reply_mode!=='text_letter')quote=await requestSetup('/toy/generation/action',{action:'billing_quote',video:preview.video_enabled===true,original:originalCharge});}
    catch(error){error.config=config;error.message=error.code==='GPU_CLIENT_UPDATE_REQUIRED'?'请重启客户端以更新原创收费确认。草稿已保留。':error.code==='GPU_INSUFFICIENT_BALANCE'?'Olivia 可用余额不足。语音或翻唱需预留 ¥1，原创单曲需预留 ¥3，视频需预留 ¥5，请先充值。草稿已保留。':'无法核对云端生成费用，请检查云端连接后重试。草稿已保留。';throw error;}
    if(quote.paid){
      const cap=(quote.max_charge_cents/100).toFixed(2);
      const priceDetail=originalCharge?'原创音乐按时长定价 ¥2–3，含小幅随机浮动，最终不超过 ¥3；视频另含视频费用，以本次总上限为准。':'成功后按实际占用结算。';
      if(!await confirmAction(`本次云端生成将从 Olivia 余额预留 ¥${cap}，本次最多收费 ¥${cap}。${priceDetail}多余预留释放；失败全退。回信文字另按 Token 计费。确认寄出？`)){
        throw Object.assign(new Error('已取消发送，草稿保留。'),{config,code:'ERR_CANCELED',__CANCEL__:true});
      }
    }
    if (once) material.route_allow_once = once;
    if (videoOnce) material.route_video_once = videoOnce;
    config.data = {...body, material};
    return config;
  };
  const mountProactiveSetting = (section) => {
    if (!document.createElement) return;
    let dirty = false;
    const container = document.createElement("div");
    container.setAttribute("data-olivia-proactive-settings", "true");
    container.className = "flex flex-col gap-3";
    const heading = text("div", "主动写信", "text-text-body text-title-m");
    const description = text(
      "p",
      "林离会在允许的时间主动写一封信。信件完成后才会进入信箱，写信期间普通寄信会暂时锁定。",
      "text-text-secondary text-body-m font-regular"
    );
    const optionStyle = document.createElement("style");
    optionStyle.textContent = `
      [data-olivia-proactive-settings] .olivia-proactive-option {
        display:flex; align-items:center; justify-content:space-between; gap:24px;
        min-height:72px; padding:16px 0; border-bottom:1px solid #343536;
        cursor:pointer; box-sizing:border-box;
      }
      [data-olivia-proactive-settings] .olivia-proactive-copy {min-width:0; display:flex; flex-direction:column; gap:6px;}
      [data-olivia-proactive-settings] .olivia-proactive-switch {
        appearance:none; -webkit-appearance:none; position:relative; flex:0 0 44px;
        width:44px; height:26px; margin:0; padding:0; border:1px solid #777772;
        border-radius:99px; background:#28292b; cursor:pointer;
      }
      [data-olivia-proactive-settings] .olivia-proactive-switch::before {
        content:''; position:absolute; top:3px; left:3px; width:18px; height:18px;
        border-radius:50%; background:#b8b6ae; transition:transform 160ms ease-out;
      }
      [data-olivia-proactive-settings] .olivia-proactive-switch:checked {background:#d9d5c9; border-color:#d9d5c9;}
      [data-olivia-proactive-settings] .olivia-proactive-switch:checked::before {transform:translateX(18px); background:#28292b;}
      [data-olivia-proactive-settings] .olivia-proactive-switch:focus-visible {outline:2px solid #eee9dd; outline-offset:4px;}
      [data-olivia-proactive-settings] .olivia-proactive-option:hover .olivia-proactive-switch {border-color:#eee9dd;}
      [data-olivia-proactive-settings] .olivia-proactive-switch:disabled {opacity:.45; cursor:default;}
      @media(prefers-reduced-motion:reduce) {[data-olivia-proactive-settings] .olivia-proactive-switch::before {transition:none;}}
    `;
    const makeOption = (label, checked, detail) => {
      const row = document.createElement("label");
      row.className = "olivia-proactive-option text-text-body text-body-m";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.className = "olivia-proactive-switch";
      input.setAttribute("role", "switch");
      input.setAttribute("aria-label", label);
      input.checked = checked;
      input.addEventListener("change", () => { void persist(input); });
      const copy = document.createElement("span");
      copy.className = "olivia-proactive-copy";
      copy.append(text("span", label, "text-text-body text-body-m"),
        text("span", detail, "text-text-secondary text-caption-m font-regular"));
      row.append(copy, input);
      return { row, input };
    };
    const enabled = makeOption("允许主动写信", proactiveState.enabled, "有合适的话题时，让林离主动给你来信。");
    const allowVoice = makeOption("主动信附带语音", proactiveState.allow_voice, "允许在主动来信中附上她的声音。");
    const loginCheck = makeOption("登录后检查来信", proactiveState.login_check_enabled, "登录 Windows 后，在后台检查是否有适合寄出的主动来信。");
    const status = text("p", "正在读取主动写信设置…", "text-text-secondary text-caption-m");
    status.setAttribute("role", "status");
    // Each switch saves on change. A failed save puts that switch back.
    const persist = async (changed) => {
      const inputs = [enabled.input, allowVoice.input, loginCheck.input];
      dirty = true;
      inputs.forEach((input) => { input.disabled = true; });
      status.textContent = "正在保存主动写信设置…";
      try {
        await saveProactiveSettings({
          enabled: enabled.input.checked,
          allow_voice: allowVoice.input.checked,
          login_check_enabled: loginCheck.input.checked,
        });
        status.textContent = "主动写信设置已保存。";
      } catch (error) {
        changed.checked = !changed.checked;
        const failures = {PROACTIVE_LOGIN_UNAVAILABLE:"登录启动暂时不可用，已恢复原设置；可以先关闭登录检查。",
          PROACTIVE_STORAGE_UNAVAILABLE:"设置没有保存，已恢复原设置。请检查磁盘空间后重试。"};
        status.textContent = failures[error?.code] || "主动写信设置没有保存，已恢复原设置，请稍后重试。";
      } finally {
        dirty = false;
        inputs.forEach((input) => { input.disabled = false; });
        reportGroupStatus(container, "proactive", enabled.input.checked ? "主动写信已开" : "主动写信已关");
      }
    };
    const render = (payload) => {
      if (dirty) return;
      enabled.input.checked = payload.enabled === true;
      allowVoice.input.checked = payload.allow_voice !== false;
      loginCheck.input.checked = payload.login_check_enabled === true;
      reportGroupStatus(container, "proactive", enabled.input.checked ? "主动写信已开" : "主动写信已关");
      if (payload.busy) {
        status.textContent = "林离正在写信。普通寄信暂时锁定。";
      } else if (payload.reason && payload.reason !== "PROACTIVE_STATUS_UNAVAILABLE") {
        const reasons = {disabled:"主动写信已关闭。", waiting:"主动写信已开启，等待合适的时间和话题。", considering:"林离在考虑要不要写信。",
          no_opportunity:"暂时没有新的话题。", deferred:"这次先不写，晚些再看看。", retry_later:"暂时未能完成检查，稍后会重试。"};
        status.textContent = reasons[payload.reason] || "主动写信已开启。";
      }
    };
    proactiveStateListeners.add(render);
    container.append(optionStyle, heading, description, enabled.row, allowVoice.row, loginCheck.row, status);
    section.append(container);
    status.textContent = proactiveState.busy ? "林离正在写信。普通寄信暂时锁定。" : "主动写信设置尚未读取。";
  };

  const mountVideoReplySetting = (section) => {
    const container = document.createElement("div");
    container.setAttribute("data-olivia-reply-routes", "true");
    container.className = "flex flex-col gap-4";
    container.append(text("div", "回信能力", "text-text-body text-title-m"),
      text("p", "林离会在允许的范围内决定怎样回信，也会听取你的明确要求。开启声音或视频，不代表每封信都会使用。", "text-text-secondary text-body-m font-regular"));
    const choices = document.createElement("div"); choices.setAttribute("role", "radiogroup"); choices.setAttribute("aria-label", "回信能力档位");
    const labels = {text:"纯文字",audio:"文字＋声音",video:"文字＋声音＋视频"};
    const descriptions = {text:"通过文字回信。",audio:"可回复文字，也可用说话、唱歌或两者组合的音频。",video:"文字、声音和视频都可使用，由本次内容决定。"};
    let selected = null, busy = false, imageEnabled = false, imageResolution = '1K';
    const imageControls = document.createElement('div'); imageControls.style.cssText='display:flex;gap:8px;align-items:center;flex-wrap:wrap';
    const imageToggle = button('图片',()=>{if(busy)return;imageEnabled=!imageEnabled;void persist();});
    imageToggle.setAttribute('aria-label','允许林离回复图片');
    const imageSizes = document.createElement('select'); imageSizes.setAttribute('aria-label','图片分辨率');
    for(const value of ['1K','2K','4K']){const option=document.createElement('option');option.value=value;option.textContent=value;imageSizes.append(option);}
    imageSizes.addEventListener('change',()=>{imageResolution=imageSizes.value;void persist();});
    imageControls.append(imageToggle,imageSizes);
    const imageHelp=text('p','图片仅云端生成，可随文字或语音回信，也适用于 QQ，每次最多 1 张。1K／2K／4K 基准价为 ¥0.50／¥0.80／¥1.10，每单随机浮动 ±10%（¥0.45–0.55／¥0.72–0.88／¥0.99–1.21）。提交时锁定并预留本单价格，重试不变价，失败释放预留。不进行图片质检；用于记忆的图片识别仍按中转用量计费。实际像素随构图变化。','text-text-secondary text-caption-m');
    const nodes = {};
    const detail = text("p", "", "text-text-secondary text-body-m font-regular");
    const status = text("p", "正在读取设置…", "text-text-secondary text-caption-m font-regular"); status.setAttribute("role", "status");
    const render = () => {
      imageToggle.disabled=busy || selected===null;imageToggle.setAttribute('aria-pressed',String(imageEnabled));
      imageSizes.hidden=!imageEnabled;imageSizes.disabled=busy;imageSizes.value=imageResolution;imageHelp.hidden=!imageEnabled;
      Object.entries(nodes).forEach(([key,node])=>{
        node.disabled=busy || selected===null; node.setAttribute("aria-checked",String(selected===key));
      });
      detail.textContent=descriptions[selected] || "";
      reportGroupStatus(container, "reply-tier", labels[selected] ? labels[selected] + (imageEnabled ? " · 允许图片" : "") : "");
    };
    Object.entries(labels).forEach(([key,label])=>{
      const choice=button(label,()=>{if(busy||selected===key)return;selected=key;void persist();});
      choice.setAttribute("role","radio"); nodes[key]=choice; choices.append(choice);
    });
    // Every choice saves immediately; a failed save restores what is stored.
    const persist=async()=>{
      busy=true;render();
      try { await routeRequest("/toy/settings/reply-routes",{request_id:videoReplyRequestId(),tier:selected,image:{enabled:imageEnabled,resolution:imageResolution}});
        status.textContent="已保存。已接收的信件继续按原设置处理。";busy=false;render(); }
      catch (_) { busy=false;await hydrate();status.textContent="没有保存成功，已恢复为原来的设置，请重试。"; }
    };
    const hydrate=async()=>{
      try {
        const result=await routeRequest("/toy/settings/reply-routes");
        selected=result.tier || (Object.values(result.routes||{}).some(Boolean) ? "video" : "text");
        imageEnabled=result.image?.enabled===true;imageResolution=result.image?.resolution||'1K';
        if(!labels[selected]) throw Error("invalid tier");
        status.textContent=result.tier_configured===false ? "当前沿用旧设置，点选一个档位即统一生效。" : "";
      } catch (_) { selected=null;status.textContent="设置读取失败，请稍后重新打开设置页。"; }
      render();
    };
    container.append(choices,detail,imageControls,imageHelp,status);section.append(container);
    refreshVideoReplySetting=()=>container.isConnected ? hydrate() : Promise.resolve(); void hydrate();
  };

  const mountMusicSettings = (section, composer=false) => {
    const panel=document.createElement("section");
    panel.setAttribute("data-olivia-music-settings","true");
    panel.style.cssText="border-top:1px solid #8884;padding-top:24px;display:flex;flex-direction:column;gap:16px";
    if(!composer)panel.append(text("h3","原创音乐","text-title-m"),text("p","调整下一首原创歌曲。保留演唱风格和参考音色，不影响翻唱。","text-text-secondary text-body-m"));
    const form=document.createElement("form");form.style.cssText="display:flex;flex-direction:column;gap:16px";
    const fields={};let defaults=null,busy=false;
    const field=(parent,name,label,type,help)=>{
      const row=document.createElement("label");row.style.cssText="display:flex;flex-direction:column;gap:8px";
      const input=document.createElement(type==="textarea"?"textarea":type==="select"?"select":"input");
      if(type!=="textarea" && type!=="select")input.type=type;
      input.name=name;fields[name]=input;
      if(type==="checkbox") {
        row.style.flexDirection="row";row.style.alignItems="flex-start";input.style.cssText="width:18px;height:18px;margin-top:4px;accent-color:#dcd3bf;flex-shrink:0";
        const copy=document.createElement("span");copy.append(text("strong",label),text("span",help,"block text-text-secondary text-caption-m"));row.append(input,copy);
      } else {
        input.style.cssText="width:100%;box-sizing:border-box;padding:12px;border-radius:12px;border:1px solid #8886;background:#111827;color:inherit;font:inherit";
        row.append(text("span",label),input);if(help)row.append(text("small",help,"text-text-secondary"));
      }
      parent.append(row);return input;
    };
    const caption=field(form,"caption","音乐描述","textarea","描述曲风、乐器、氛围和唱法；留空由林离根据回信安排曲风和唱法；填写后优先使用你的描述，歌词仍由信件生成。");
    caption.rows=3;caption.maxLength=1000;caption.placeholder="例如：舒缓的钢琴民谣，自然轻声演唱，副歌温暖舒展";
    const advanced=document.createElement('details');advanced.append(text('summary','进阶音乐参数'));form.append(advanced);
    const title=field(advanced,'title','歌曲标题','text','留空使用默认标题');title.maxLength=80;
    const duration=field(advanced,'duration','目标时长（秒）','number','留空随机 180–270 秒；填写 180–270，实际时长以生成结果为准。');duration.min=180;duration.max=270;duration.step=1;
    const negative=field(advanced,'negative_tags','避免的风格或元素','textarea','例如：重金属、尖锐高音；多个项目用逗号分隔。');negative.maxLength=1000;
    for(const [name,label,help] of [['style_weight','风格遵循度','0–1，越高越贴近音乐描述；留空使用服务默认值。'],['weirdness_constraint','创意偏离度','0–1，越高越允许偏离常规；留空使用服务默认值。']]){const input=field(advanced,name,label,'number',help);input.min=0;input.max=1;input.step=0.01;}
    const status=text("p","正在读取音乐设置…","text-text-secondary text-body-m");status.setAttribute("role","status");
    const controls=actions();
    const fill=options=>{for(const [name,input] of Object.entries(fields)){if(input.type==="checkbox")input.checked=options[name];else input.value=options[name]??"";}};
    const setBusy=value=>{busy=value;for(const el of form.querySelectorAll("input,textarea,select,button"))el.disabled=value;};
    const load=async()=>{
      if(busy)return;setBusy(true);
      try{if(!setupSessionToken)await requestSetup(SETUP_STATUS_PATH);
        const result=await requestSetup("/toy/generation/action",{action:"music_settings_status"});defaults=result.defaults;
        const durationHint=result.original_music_provider==='suno_v6'?"默认目标时长随机为3–4分半，可在进阶设置指定，实际时长以生成结果为准。原创单曲按时长收费 ¥2–3，含小幅随机浮动，最高 ¥3。":result.original_music_provider==='unavailable'?"暂时无法确认服务时长，请连接后重新读取。":"当前服务沿用约 110 秒原创方案。";
        fill(result.options||defaults);status.textContent=result.error_code?"原设置无法读取，请检查参数。":durationHint+"音乐描述随"+(composer?"这封信":"下一首原创歌曲")+"生效。";
      }catch(_){status.textContent="音乐设置读取失败，请重新读取。";}finally{setBusy(false);save.disabled=!defaults;}
    };
    const save=button("保存音乐设置",async()=>{
      if(busy||!defaults||!form.reportValidity())return;
      const options={};for(const [name,input] of Object.entries(fields))options[name]=input.type==="checkbox"?input.checked:input.type==="number"?(input.value.trim()===""?null:Number(input.value)):input.value;
      setBusy(true);
      try{await requestSetup("/toy/generation/action",{action:"music_settings_save",options});status.textContent="已保存，对下一首原创歌曲生效。";}
      catch(_){status.textContent="保存失败，请检查参数后重试；原设置未改动。";}finally{setBusy(false);}
    });save.disabled=true;
    if(!composer)controls.append(save);
    controls.append(button("恢复推荐参数",()=>{if(!busy&&defaults){fill(defaults);status.textContent=composer?"已恢复推荐参数。":"已填入推荐参数，保存后生效。";}}));
    if(!composer)controls.append(button("重新读取",load));
    form.addEventListener("submit",event=>event.preventDefault());form.append(controls,status);panel.append(form);section.append(panel);void load();
    return {read:()=>{if(busy||!defaults)throw Error('音乐参数尚未读取，请稍候或重新打开写信窗口。');if(!form.reportValidity())throw Error('请检查音乐参数。');const options={};for(const [name,input] of Object.entries(fields))options[name]=input.type==='checkbox'?input.checked:input.type==='number'?(input.value.trim()===""?null:Number(input.value)):input.value;return options;}};
  };

  const mountDiagnosticExport = (section) => {
    const row = document.createElement("div");
    row.className = "flex items-center justify-between px-0 py-3 rounded-3";
    const copy = document.createElement("div");
    copy.className = "flex flex-col gap-0 flex-1 min-w-0";
    const state = text("div", "导出本机脱敏诊断包，文件仅保存到本地。", "text-text-secondary text-caption-m font-regular");
    copy.append(state);
    const exportButton = button("导出诊断包", async () => {
      setDiagnosticDetails(state, []);
      setButtonsBusy([exportButton], true);
      state.textContent = "正在生成诊断包…";
      try {
        const blob = await requestDiagnosticExport();
        const url = URL.createObjectURL(blob);
        const download = document.createElement("a");
        download.href = url;
        download.download = "olivia-diagnostic-bundle.zip";
        download.style.display = "none";
        document.body.append(download);
        download.click();
        download.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 0);
        state.textContent = "诊断包已保存到本地下载位置。";
      } catch (error) {
        const code = error && typeof error.code === "string"
          ? error.code
          : "DIAGNOSTIC_EXPORT_UNAVAILABLE";
        state.textContent = "诊断包未能生成。请稍后再次点击导出。";
        setDiagnosticDetails(state, code);
      } finally {
        setButtonsBusy([exportButton], false);
      }
    });
    row.append(copy, exportButton);
    section.append(text("div", "诊断与反馈", "text-text-body text-title-m"), row);
  };

  const showLocalImportProgress = (state, home) => {
    document.querySelector('[data-olivia-local-import-progress]')?.remove();
    const backdrop = document.createElement("div");
    backdrop.setAttribute("data-olivia-local-import-progress", "");
    Object.assign(backdrop.style, {position:"fixed", inset:"0", zIndex:"2147483001",
      display:"grid", placeItems:"center", padding:"24px", background:"rgba(0,0,0,.62)",
      pointerEvents:"auto", webkitAppRegion:"no-drag"});
    const dialog = document.createElement("section");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-label", "本地备份导入进度");
    Object.assign(dialog.style, {width:"min(560px, calc(100vw - 48px))", padding:"24px",
      borderRadius:"12px", background:"#18191c", color:"#f9fafb", display:"grid", gap:"16px"});
    const dismiss = () => { home.append(state); backdrop.remove(); };
    const close = button("关闭进度窗口", dismiss);
    dialog.append(text("h3", "本地备份导入进度"), state,
      text("p", "关闭窗口不会停止后台导入，可再次点击“查看导入进度”。"), close);
    backdrop.append(dialog);
    backdrop.addEventListener("click", event => {
      if (event.target === backdrop) event.preventDefault();
    });
    backdrop.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); dismiss(); }
    });
    document.body.append(backdrop);
    close.focus();
  };

  const mountHistoryRelationship = (section) => {
    const state = text("div", "正在读取历史关系评估进度……", "text-text-secondary text-body-m");
    const refresh = async () => {
      try {
        const result = await requestJson(LOCAL_LETTER_IMPORT_PATH, {relationship:"1"});
        const count = `${result.processed || 0} / ${result.total || 0} 组`;
        state.textContent = result.status === "RUNNING" ? `历史关系评估中：${count}，按顺序每五组往返记录评估一次。`
          : result.status === "FAILED" ? `历史关系评估暂停：${count}。已保存的原文不受影响，可重试剩余批次。`
          : result.status === "PENDING" ? `历史关系等待评估：${count}。请配置可用的大模型后点击重试。`
          : result.status === "APPLIED" ? `历史关系已评估：${count}。重复记录不重复评估。`
          : "历史关系评估暂不可用。";
        setDiagnosticDetails(state, result.status === "FAILED" ? result.error_code : null);
        // The evaluation runs by itself; the button is only for a paused or waiting run.
        retry.hidden = !["FAILED", "PENDING"].includes(result.status);
        reportGroupStatus(state, "relationship", result.status === "RUNNING" ? `正在评估历史关系 ${count}` : "");
        reportGroupStatus(state, "relationship:problem", result.status === "FAILED" ? "历史关系评估暂停，需要重试" : "");
        if (result.status === "RUNNING" && state.isConnected) window.setTimeout(refresh, 2000);
      } catch (_) { state.textContent = "暂时无法读取历史关系进度，可点击重试。"; retry.hidden = false; }
    };
    const retry = button("评估历史关系 / 重试", async () => {
      retry.disabled = true;
      try {
        await requestMutation("/toy/letter/backup/import", {relationship_retry:true});
        await refresh();
      } catch (_) { state.textContent = "暂时无法启动历史关系评估，请重试。"; }
      finally { retry.disabled = false; }
    });
    retry.hidden = true;
    section.append(text("div", "历史关系", "text-text-body text-title-m"),
      text("p", "原文保存后会按顺序每五组完整往返信件或聊天评估关系，调用已配置的大模型并消耗额度。只有单方文字的记录不评估关系；失败后暂停，重试会接着未完成的批次。", "text-text-secondary text-body-m font-regular"), state, retry);
    void refresh();
  };

  const readLetterBackupFile = async (selected) => {
    let backup;
    if (/\.soul$/i.test(selected.name || "")) {
      const head = await selected.slice(0, 16).arrayBuffer();
      if (head.byteLength !== 16 || new TextDecoder().decode(head.slice(0, 8)) !== "SOUL0001") throw Error("invalid soul");
      const view = new DataView(head), length = view.getUint32(8, true);
      if (view.getUint32(12, true) !== 0 || !length || length > 16 * 1024 * 1024 || length + 16 > selected.size) throw Error("invalid soul size");
      const manifest = JSON.parse(new TextDecoder("utf-8", {fatal:true}).decode(await selected.slice(16, 16 + length).arrayBuffer()));
      if (!Array.isArray(manifest?.memory?.exchanges)) throw Error("invalid soul exchanges");
      backup = {format:"soul", manifest:{memory:{exchanges:manifest.memory.exchanges.map(row => {
        if (!row || typeof row !== "object" || Array.isArray(row)) throw Error("invalid soul exchange");
        const {incoming, reply, date, time} = row;
        return {incoming, reply, date, time};
      })}}};
    } else {
      if (selected.size > 16 * 1024 * 1024) throw Error("backup too large");
      backup = (await selected.text()).replace(/^\uFEFF/, "");
    }
    if (new TextEncoder().encode(JSON.stringify({backup})).length > 16 * 1024 * 1024 + 1024) throw Error("backup too large");
    return backup;
  };

  const mountLetterBackup = (section) => {
    const state = text("div", "备份包含信件、QQ／微信聊天的文字和时间。QQ／微信只保存已确认发出的回复；不含图片或音视频附件。请自行保管内容。", "text-text-secondary text-body-m font-regular");
    state.setAttribute("aria-live", "polite");
    const controls = actions();
    const file = document.createElement("input");
    file.type = "file"; file.accept = ".json,.soul,application/json"; file.hidden = true;
    file.addEventListener("cancel", event => event.stopPropagation());
    const save = button("导出信件与聊天备份", async () => {
      setButtonsBusy([save, restore], true);
      state.textContent = "正在导出信件与聊天原文……";
      try {
        const result = await requestMutation("/toy/letter/backup/export", {});
        if (result.status !== "READY" || result.backup?.schema_version !== "olivia.letters.v1" || !Array.isArray(result.backup.letters)) throw Error("invalid backup");
        const blob = new Blob([JSON.stringify(result.backup, null, 2)], {type:"application/json;charset=utf-8"});
        const url = URL.createObjectURL(blob), link = document.createElement("a");
        link.href = url; link.download = `Olivia-letters-${new Date().toISOString().slice(0,10)}.json`;
        document.body.append(link); link.click(); link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        const qq = result.backup.letters.filter(row => row.channel === "qq").length;
        const wechat = result.backup.letters.filter(row => row.channel === "wechat").length;
        state.textContent = `已导出 ${result.backup.letters.length - qq - wechat} 封信件、${qq} 条 QQ 和 ${wechat} 条微信聊天记录，请在下载位置查看。`;
      } catch (_) { state.textContent = "备份导出失败，请重试。原记录未改变。"; }
      finally { setButtonsBusy([save, restore], false); }
    });
    const restore = button("选择文件导入", () => file.click());
    file.addEventListener("change", async () => {
      const selected = file.files?.[0]; if (!selected) return;
      setButtonsBusy([save, restore], true);
      try {
        const backup = await readLetterBackupFile(selected);
        if (!await confirmAction("导入所选文件中的信件与聊天记录？支持灵离 .soul、原版 letter_pairs.json 和 Olivia 文字备份；QQ／微信记录需来自 Olivia 备份。只读保存文字，不回发旧消息，不导入图片或音视频；重复记录跳过，已有记录不覆盖。随后每五组完整往返记录调用模型评估关系并消耗额度，已有进度保留。")) return;
        state.textContent = "正在保存信件与聊天原文，无需等待大模型……";
        const result = await requestMutation("/toy/letter/backup/import", {backup});
        if (result.status !== "APPLIED") throw Error("import failed");
        state.textContent = `已导入 ${result.inserted} 条记录，重复 ${result.duplicates} 条。正在刷新历史记录。`;
        window.setTimeout(() => window.location.reload(), 800);
      } catch (_) { state.textContent = "导入未完成。请选择完整的 .soul、letter_pairs.json 或 Olivia 文字备份（文字清单最大 16 MB）；可再次导入，重复记录会跳过。"; }
      finally { file.value = ""; setButtonsBusy([save, restore], false); }
    });
    controls.append(save, restore, file);
    section.append(text("div", "导入与导出信件及聊天", "text-text-body text-title-m"),
      text("p", "Olivia JSON 备份可恢复信件与 QQ／微信文字聊天；.soul 和 letter_pairs.json 只恢复文件内的信件文字。换电脑请先导出备份，再点“选择文件导入”。没有单独保存文件时，可在下方从原版目录读取信件。", "text-text-secondary text-body-m font-regular"), state, controls);
  };

  const mountLetterMaintenance = (section) => {
    const box = document.createElement("details");
    box.className = "olivia-letter-maintenance";
    const summaryNode = text("summary", "信件对比与整理");
    summaryNode.style.cssText = "cursor:pointer;padding:8px 0;color:var(--tp-text-body);font-size:18px;font-weight:var(--tp-font-weight-medium)";
    box.append(summaryNode);
    const style = text("style", `
      .olivia-letter-maintenance{margin:24px 0;border-top:1px solid #424242;padding-top:20px;color:inherit}
      .olivia-letter-maintenance summary{cursor:pointer;font-weight:600;padding:8px 0;font-size:18px}
      .olivia-letter-maintenance p{line-height:1.65;margin:12px 0;max-width:72ch}
      .olivia-letter-maintenance .lm-controls{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}
      .olivia-letter-maintenance .lm-row{padding:18px 0;border-top:1px solid #424242}
      .olivia-letter-maintenance .lm-pair{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:20px;margin:12px 0}
      .olivia-letter-maintenance .lm-copy{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7;max-height:260px;overflow:auto;margin:8px 0;color:var(--tp-text-body)}
      .olivia-letter-maintenance .lm-column{min-width:0}
      .olivia-letter-maintenance label{display:flex;flex-wrap:wrap;align-items:center;gap:10px}
      .olivia-letter-maintenance select{background:#242424;color:#eee;border:1px solid #777;border-radius:6px;padding:8px;max-width:100%}
      .olivia-letter-maintenance :focus-visible{outline:2px solid currentColor;outline-offset:3px}
      .olivia-letter-maintenance [hidden]{display:none!important}
      .olivia-letter-maintenance button:disabled,.olivia-letter-maintenance select:disabled{opacity:.5;cursor:default}
    `);
    const help = text("p", "先检查，再选择要整理的信件。可以对比 .soul 或 JSON 备份、修复时间和顺序、收起重复信与失败信。整理只影响信箱显示，不改原文或长期记忆，也不调用模型；可在这里恢复。", "text-text-secondary text-body-m font-regular");
    const status = text("p", "尚未检查。检查当前信箱无需选择文件。", "text-text-secondary text-body-m font-regular");
    status.setAttribute("aria-live", "polite");
    const controls = actions(); controls.className = "lm-controls";
    const results = document.createElement("div"), pager = actions(); pager.className = "lm-controls";
    const file = document.createElement("input"); file.type = "file"; file.accept = ".json,.soul,application/json"; file.hidden = true;
    file.addEventListener("cancel", event => event.stopPropagation());
    let backup = null, filename = "", plan = null, page = 0, busy = false, selectors = [];
    const labels = {duplicate:"完全重复", near:"近似重复 · 请核对两侧正文", time:"可修复时间与顺序", failed:"失败或已取消的信件",
      imported:"旧工具导入信", restore:"已整理 · 可恢复", same:"内容一致", missing:"仅备份中存在 · 可用上方入口导入",
      source_near:"正文有差异 · 仅对比，不自动覆盖或修复时间", ambiguous:"备份存在多个日期 · 暂不修复", library_only:"仅信箱中存在"};
    const selected = () => selectors.map(el => el.value).filter(Boolean);
    const sync = () => {
      setButtonsBusy([scan, compare, previous, next], busy);
      selectors.forEach(el => el.disabled = busy);
      apply.disabled = busy || !plan || !selected().length;
      apply.textContent = `应用所选（${selected().length}）`;
      previous.disabled = busy || page === 0;
      next.disabled = busy || !plan || (page + 1) * 30 >= plan.total;
    };
    const failure = error => {
      const messages = {LETTER_MAINTENANCE_STALE:"信箱已变化，请重新检查后选择。", MEMORY_ADMIN_BUSY:"正在导入或整理记忆，请稍后重新检查。",
        LETTER_BACKUP_STORAGE_UNAVAILABLE:"无法保存信箱整理结果，请检查磁盘后重试。", LETTER_MAINTENANCE_INVALID:"所选操作有冲突或备份格式无效，请重新检查；不要同时收起需要保留的信件。"};
      status.textContent = messages[error?.code] || "检查或整理未完成，请重试。可重新打开此入口检查实际状态。";
    };
    const column = (row, title) => {
      const col = document.createElement("div"); col.className = "lm-column";
      let date = "时间未知";
      if (row.created_at != null) {
        const parsed = new Date(typeof row.created_at === "number" ? row.created_at * 1000 : row.created_at);
        if (!Number.isNaN(parsed.getTime())) date = parsed.toLocaleString("zh-CN", {timeZone:"Asia/Shanghai"}) + "（北京时间）";
      }
      const content = text("div", `你的信：\n${row.content || "（空）"}\n\n回信：\n${row.reply_text || "（空）"}`, "lm-copy");
      col.append(text("strong", title), text("p", date), content);
      if (row.truncated) {
        const full = button("查看完整正文", async () => {
          full.disabled = true;
          try {
            const data = await requestMutation("/toy/letter/maintenance/detail", {backup, key:row.key});
            content.textContent = `你的信：\n${data.content || "（空）"}\n\n回信：\n${data.reply_text || "（空）"}`;
            full.hidden = true;
          } catch (error) { failure(error); full.disabled = false; }
        });
        col.append(full);
      }
      return col;
    };
    const render = () => {
      results.replaceChildren(); selectors = [];
      for (const item of plan.items) {
        const row = document.createElement("div"); row.className = "lm-row";
        row.append(text("strong", labels[item.kind] || item.kind, "text-text-body text-label-l"));
        const pair = document.createElement("div"); pair.className = "lm-pair";
        const sourceLeft = ["missing", "source_near", "ambiguous"].includes(item.kind);
        pair.append(column(item.left, sourceLeft ? "备份中的信件" : "信箱中的信件"));
        if (item.right) pair.append(column(item.right, ["same", "time"].includes(item.kind) ? "备份中的信件" : "信箱中的另一封"));
        row.append(pair);
        if (item.options.length) {
          const label = text("label", "处理方式", "text-text-secondary text-body-m"), select = document.createElement("select");
          const skip = text("option", "保持原样"); skip.value = ""; select.append(skip);
          item.options.forEach(option => { const el = text("option", option.label); el.value = option.id; select.append(el); });
          select.addEventListener("change", sync); selectors.push(select); label.append(select); row.append(label);
        }
        results.append(row);
      }
      pager.hidden = plan.total <= 30;
      pageLabel.textContent = `第 ${page + 1} 页 / ${Math.max(1, Math.ceil(plan.total / 30))} 页`;
    };
    const inspect = async (targetPage = 0) => {
      if (busy) return;
      busy = true; plan = null; selectors = []; results.replaceChildren(); sync();
      status.textContent = "正在检查信件，只读取本地内容……";
      try {
        plan = await requestMutation("/toy/letter/maintenance/preview", {backup, page:targetPage});
        page = targetPage; render();
        const counts = plan.counts;
        status.textContent = `${filename ? `对比文件：${filename}。` : "当前信箱："}共 ${plan.total} 项；完全重复 ${counts.duplicate || 0}，近似重复 ${counts.near || 0}，可修复时间 ${counts.time || 0}，失败信 ${counts.failed || 0}，可恢复 ${counts.restore || 0}。${plan.total ? "选择处理方式后点击应用。翻页会清空本页选择，请先应用。" : "没有需要整理的信件。"}${plan.near_limited ? "信件较多，本次仅完成部分近似对比；完全重复检查已覆盖全部。" : ""}`;
      } catch (error) { failure(error); }
      finally { busy = false; sync(); }
    };
    const scan = button("检查当前信箱", () => { backup = null; filename = ""; return inspect(); });
    const compare = button("选择备份对比 / 修复时间", () => file.click());
    const apply = button("应用所选（0）", async () => {
      const ids = selected(); if (busy || !plan || !ids.length) return;
      if (!await confirmAction(`应用所选的 ${ids.length} 项整理？收起的信件会从信箱移除显示，原文仍保留，可在此恢复。时间修复按所选备份执行，不会改写正文。`)) return;
      busy = true; sync(); status.textContent = "正在保存整理结果……";
      try {
        await requestMutation("/toy/letter/maintenance/apply", {backup, token:plan.token, selected:ids});
        busy = false; await inspect();
        status.textContent = `已应用 ${ids.length} 项。重新进入信箱即可查看。` + status.textContent;
      } catch (error) { failure(error); plan = null; }
      finally { busy = false; sync(); }
    });
    const previous = button("上一页", () => inspect(page - 1)), next = button("下一页", () => inspect(page + 1));
    const pageLabel = text("span", "");
    file.addEventListener("change", async () => {
      const chosen = file.files?.[0]; if (!chosen || busy) return;
      busy = true; sync();
      try { backup = await readLetterBackupFile(chosen); filename = chosen.name; busy = false; await inspect(); }
      catch (_) { plan = null; results.replaceChildren(); selectors = []; status.textContent = "备份无法读取。请选择完整的 .soul 或 JSON 文件（文字清单最大 16 MB）。"; }
      finally { file.value = ""; busy = false; sync(); }
    });
    controls.append(scan, compare, apply, file); pager.append(previous, pageLabel, next); pager.hidden = true;
    box.append(style, help, controls, status, results, pager); section.append(box); sync();
  };

  const mountLocalLetterImport = (section) => {
    mountLetterBackup(section);
    mountLetterMaintenance(section);
    const importRow = document.createElement("div");
    importRow.className = "flex items-center justify-between px-0 py-3 rounded-3";
    const importCopy = document.createElement("div");
    importCopy.className = "flex flex-col gap-0 flex-1 min-w-0";
    const importState = text("div", "", "text-text-secondary text-caption-m font-regular");
    importState.setAttribute("aria-live", "polite");
    importCopy.append(
      text("div", "从原版目录读取（另一种导入方式）", "text-text-body text-label-l"),
      text("div", "读取安装时选择的原版游戏目录中的 letter_pairs.json。双方原文作为只读历史进入信箱并可供检索，保存原文不联网、不导入视频。随后每五封按顺序调用模型评估关系并消耗额度；重复信件和已完成批次跳过。", "text-text-secondary text-body-m font-regular"),
      importState
    );
    let importPending = false;
    const missingBackupText = "未在原版游戏目录找到 letter_pairs.json。官方服务器已关闭，请先准备本地备份并放回该目录。";
    const refreshLocalBackup = async () => {
      setDiagnosticDetails(importState, []);
      try {
        const payload = await requestJson(LOCAL_LETTER_IMPORT_PATH);
        importState.textContent = `已找到本地备份，共 ${payload.seen} 封；可新增 ${payload.would_insert} 封，需修复 ${payload.would_update} 封，清理旧乱码重复 ${payload.would_remove} 封，重复 ${payload.duplicates} 封。`;
        return payload;
      } catch (error) {
        setDiagnosticDetails(importState, error?.code);
        importState.textContent = error && error.code === "OFFLINE_LETTER_BACKUP_REQUIRED"
          ? missingBackupText
          : error && error.code === "OFFLINE_LETTER_BACKUP_INVALID"
            ? "本地 letter_pairs.json 格式无效，请更换完整备份后重试。"
            : "暂时无法检查本地备份，请重启 Olivia 后重试。";
        return null;
      }
    };
    const importButton = button("从原版目录读取", async () => {
      if (importPending) {
        showLocalImportProgress(importState, importCopy);
        return;
      }
      const preflight = await refreshLocalBackup();
      if (!preflight) return;
      const changeCount = preflight.would_insert + preflight.would_update + preflight.would_remove;
      if (!await confirmAction(`确认从本地 letter_pairs.json 写入或修复 ${changeCount} 封只读历史信件？保留双方原文，随后每五封调用模型评估关系并消耗额度，已完成批次跳过。`)) {
        return;
      }
      importButton.textContent = "查看导入进度";
      importPending = true;
      importState.textContent = "正在读取本地备份并写入信箱……";
      showLocalImportProgress(importState, importCopy);
      try {
        let payload = await requestJson(LOCAL_LETTER_IMPORT_PATH, {progress: "1"});
        if (payload.status !== "RUNNING") {
          payload = await requestMutation(LOCAL_LETTER_IMPORT_PATH, {background: true, originals_only: true});
        }
        while (payload.status === "RUNNING") {
          const stages = {preflight: "检查备份", memory: "整理长期记忆", relationship: "整理关系状态"};
          importState.textContent = payload.stage === "memory_wait"
            ? "正在准备长期记忆，就绪后会自动继续导入。无需重复提交；关闭此面板不会停止任务。"
            : `${stages[payload.stage] || "后台导入中"}：${payload.processed || 0} / ${payload.total || 0}。请勿重复提交；关闭此面板不会停止任务。`;
          await new Promise(resolve => window.setTimeout(resolve, 2000));
          payload = await requestJson(LOCAL_LETTER_IMPORT_PATH, {progress: "1"});
        }
        if (payload.status !== "APPLIED") {
          const failure = new Error("import-failed");
          failure.code = payload.error_code || "OFFLINE_HISTORY_IMPORT_FAILED";
          throw failure;
        }
        const inserted = Number.isInteger(payload.inserted) ? payload.inserted : 0;
        const updated = Number.isInteger(payload.updated) ? payload.updated : 0;
        const removed = Number.isInteger(payload.removed) ? payload.removed : 0;
        const duplicates = Number.isInteger(payload.duplicates) ? payload.duplicates : 0;
        const migration = payload.memory_migration || {};
        const memoryWritten = Number.isInteger(migration.written) ? migration.written : 0;
        const memoryDuplicates = Number.isInteger(migration.duplicates) ? migration.duplicates : 0;
        const memorySkipped = Number.isInteger(migration.skipped) ? migration.skipped : 0;
        importState.textContent = payload.memory_mode === "originals"
          ? `已导入 ${inserted} 封、修复 ${updated} 封、清理重复 ${removed} 封、跳过重复 ${duplicates} 封。双方原文已保存；关系评估在后台按五封一批继续。正在刷新信箱。`
          : `已导入 ${inserted} 封、修复 ${updated} 封；记忆提取 ${memoryWritten} 封，已有记忆 ${memoryDuplicates} 封，未提取 ${memorySkipped} 封。正在刷新信箱。`;
        importButton.textContent = "已完成";
        window.setTimeout(() => {
          try { window.location.reload(); } catch (_error) { /* native shell may own navigation */ }
        }, 800);
      } catch (error) {
        setDiagnosticDetails(importState, error?.code);
        importState.textContent = error && error.code === "OFFLINE_LETTER_BACKUP_REQUIRED"
          ? missingBackupText
          : error && error.code === "OFFLINE_LETTER_BACKUP_INVALID"
            ? "本地 letter_pairs.json 格式无效，请更换完整备份后重试。"
            : error && error.code === "OFFICIAL_HISTORY_MEMORY_WAIT_TIMEOUT"
              ? "长期记忆准备时间较长，本次尚未开始导入。请等待长期记忆显示可用后，再点击导入。"
            : error && error.code === "OFFICIAL_HISTORY_MEMORY_UNAVAILABLE"
              ? "长期记忆尚不可用，本次尚未开始导入。请到长期记忆页面查看状态；恢复可用后再点击导入。"
            : error && error.code && /^[A-Z][A-Z0-9_]{0,95}$/.test(error.code)
              ? "本地信件导入未完成。请检查备份文件和回信服务连接，然后点击重试。"
              : "暂时无法读取导入结果，后台任务可能仍在继续。点击可查询进度，请勿重启或重复导入。";
        importButton.textContent = "查看进度 / 导入";
      } finally {
        importPending = false;
        setButtonsBusy([importButton], false);
      }
    });
    importRow.append(importCopy, importButton);
    section.append(importRow);
    mountHistoryRelationship(section);
  };

  const mountShell = () => {
    if (!isSettingsRoute()) {
      removeShell();
      return;
    }
    if (document.querySelector(`[${ROOT_ATTR}]`)) {
      return;
    }
    const container = findSettingsContainer();
    if (!container) {
      return;
    }

    const section = document.createElement("div");
    section.setAttribute(ROOT_ATTR, "");
    section.className = "tp-settings-item";
    section.append(groupStyle(), text("div", "Olivia", "text-text-body text-title-m"));

    const reply = settingsGroup("reply", "林离怎么回复", "回信方式、图片和主动写信");
    mountVideoReplySetting(reply);
    if (window.__oliviaNativeView) mountProactiveSetting(reply);
    const chat = settingsGroup("chat", "QQ / 微信", "绑定后可以在 QQ 或微信里和林离聊天");
    const letters = settingsGroup("letters", "信件与记忆", "备份信件与聊天，查看长期记忆");
    const memoryRow = document.createElement("div");
    memoryRow.className = "olivia-group-row";
    memoryRow.append(text("span", "长期记忆：查看、搜索和更正林离记住的事", "text-text-body text-body-m"),
      button("打开", () => openDialog(false, "memory")));
    letters.append(memoryRow);
    mountLocalLetterImport(letters);
    const help = settingsGroup("help", "更新与帮助", "补丁更新和诊断包");
    const update = document.createElement("div");
    update.className = "flex flex-col gap-3";
    renderLocalUpdatePanel(update);
    help.append(update);
    mountDiagnosticExport(help);
    requestJson("/toy/updates/local/status").then((value) => {
      reportGroupStatus(help, "version", typeof value.version === "string" ? `当前 ${value.version}` : "基础安装版");
    }).catch(() => {});

    section.append(...[reply, chat, letters, help].map((body) => body.oliviaGroup));
    container.append(section);
  };

  // One collapsible block per topic. The summary line shows what the contents
  // report through "olivia-group-status" events, so blocks stay independent.
  const settingsGroup = (id, title, fallback) => {
    const group = document.createElement("details");
    group.name = "olivia-settings-group";
    group.className = "olivia-group";
    group.setAttribute("data-olivia-group", id);
    const summary = document.createElement("summary");
    const copy = document.createElement("span");
    copy.className = "olivia-group-copy";
    const line = text("span", fallback, "text-text-secondary text-body-m font-regular");
    line.setAttribute("data-olivia-group-status", "");
    copy.append(text("span", title, "text-text-body text-label-l"), line);
    summary.append(copy, text("span", "›", "olivia-group-chevron"));
    const body = document.createElement("div");
    body.className = "olivia-group-body";
    body.setAttribute("data-olivia-group-body", id);
    const parts = new Map();
    group.addEventListener("olivia-group-status", (event) => {
      const {part, value} = event.detail || {};
      if (!part) return;
      if (value) parts.set(part, value); else parts.delete(part);
      line.textContent = parts.size ? Array.from(parts.values()).join(" · ") : fallback;
      line.setAttribute("data-state", Array.from(parts.keys()).some((key) => key.endsWith(":problem")) ? "problem" : "");
    });
    group.append(summary, body);
    body.oliviaGroup = group;
    return body;
  };

  const groupStyle = () => {
    const style = document.createElement("style");
    style.textContent = `
      [${ROOT_ATTR}] .olivia-group{border-top:1px solid #343536}
      [${ROOT_ATTR}] .olivia-group > summary{list-style:none;display:flex;align-items:center;gap:16px;padding:16px 0;cursor:pointer}
      [${ROOT_ATTR}] .olivia-group > summary::-webkit-details-marker{display:none}
      [${ROOT_ATTR}] .olivia-group-copy{display:flex;flex-direction:column;gap:4px;flex:1;min-width:0}
      [${ROOT_ATTR}] .olivia-group-chevron{font-size:20px;color:#8b8d92;transition:transform 160ms ease-out}
      [${ROOT_ATTR}] .olivia-group[open] .olivia-group-chevron{transform:rotate(90deg)}
      [${ROOT_ATTR}] .olivia-group > summary:focus-visible{outline:2px solid #eee9dd;outline-offset:4px;border-radius:8px}
      [${ROOT_ATTR}] [data-olivia-group-status][data-state="problem"]{color:#f0a19a}
      [${ROOT_ATTR}] .olivia-group-body{display:flex;flex-direction:column;gap:24px;padding:4px 0 24px}
      [${ROOT_ATTR}] .olivia-group-row{display:flex;align-items:center;justify-content:space-between;gap:16px}
      [${ROOT_ATTR}] .olivia-group-advanced > summary{cursor:pointer;color:#acb0b4;font-size:13px}
      [${ROOT_ATTR}] .olivia-group-advanced[open] > summary{margin-bottom:12px}
      [${ROOT_ATTR}] .olivia-group-body button{appearance:none;display:inline-flex;align-items:center;justify-content:center;gap:6px;
        width:auto;align-self:flex-start;min-height:36px;padding:0 18px;border:1px solid #4a4c51;border-radius:999px;background:#232427;color:#ece9e2;
        font:inherit;font-size:14px;line-height:20px;cursor:pointer;transition:background-color 120ms ease-out,border-color 120ms ease-out}
      [${ROOT_ATTR}] .olivia-group-body button:hover:not(:disabled){background:#2e2f33;border-color:#6b6d72}
      [${ROOT_ATTR}] .olivia-group-body button:focus-visible{outline:2px solid #eee9dd;outline-offset:2px}
      [${ROOT_ATTR}] .olivia-group-body button:disabled{opacity:.45;cursor:default}
      [${ROOT_ATTR}] .olivia-group-body [role="radiogroup"]{display:flex;gap:0 !important;padding:3px;border:1px solid #4a4c51;border-radius:12px;background:#1d1e21}
      [${ROOT_ATTR}] .olivia-group-body [role="radiogroup"] button{flex:1 1 0 !important;border:0;border-radius:9px;background:transparent;color:#c9c7c1}
      [${ROOT_ATTR}] .olivia-group-body [role="radiogroup"] button[aria-checked="true"]{background:#ded7cb !important;color:#18191b !important}
      [${ROOT_ATTR}] .olivia-group-body button[aria-pressed="true"]{background:#ded7cb !important;border-color:#ded7cb;color:#18191b !important}
      [${ROOT_ATTR}] .olivia-group-body select{appearance:none;min-height:36px;padding:0 32px 0 14px;border:1px solid #4a4c51;border-radius:999px;
        background:#232427 url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23acb0b4' stroke-width='2'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E") no-repeat right 12px center;
        color:#ece9e2;font:inherit;font-size:14px;cursor:pointer}
      [${ROOT_ATTR}] .olivia-group-body details > summary{list-style:none;cursor:pointer;color:#acb0b4;font-size:13px}
      [${ROOT_ATTR}] .olivia-group-body details > summary::-webkit-details-marker{display:none}
      [${ROOT_ATTR}] .olivia-group-body details > summary::before{content:"›";display:inline-block;width:14px;transition:transform 160ms ease-out}
      [${ROOT_ATTR}] .olivia-group-body details[open] > summary::before{transform:rotate(90deg)}
      @media(prefers-reduced-motion:reduce){[${ROOT_ATTR}] .olivia-group-chevron,[${ROOT_ATTR}] .olivia-group-body button{transition:none}}
    `;
    return style;
  };

  const reportGroupStatus = (node, part, value) => {
    if (typeof CustomEvent !== "function" || typeof node?.dispatchEvent !== "function") return;
    node.dispatchEvent(new CustomEvent("olivia-group-status", {bubbles: true, detail: {part, value}}));
  };

  let setupCheckPending = false;
  let setupPoll = null;
  const maybeOpenInitialSetup = async () => {
    if (
      setupCheckPending
      || document.querySelector(`[${DIALOG_ATTR}]`)
    ) {
      return;
    }
    setupCheckPending = true;
    try {
      const payload = await requestSetup(SETUP_STATUS_PATH);
      if (payload.setup_completed && setupPoll !== null) {
        window.clearInterval(setupPoll);
        setupPoll = null;
      } else if (payload.show_initial_setup) {
        openDialog(true);
      }
    } catch (_error) {
      // The local service may still be starting; the bounded poll retries later.
    } finally {
      setupCheckPending = false;
    }
  };

  // Balance on the title-bar account button, once a key is connected. It is
  // refreshed when the account dialog closes.
  const refreshAccountEntry = async () => {
    const entry = document.querySelector('[data-olivia-account-entry]');
    if (!entry) return;
    try {
      if (!setupSessionToken) await requestSetup(SETUP_STATUS_PATH);
      const data = await requestSetup("/toy/generation/action", {action: "settings_status"});
      if (!data.has_key) { entry.textContent = '账户'; return; }
      const account = await requestSetup("/toy/generation/action", {action: "billing_statement"});
      const value = Number(account.remaining_yuan);
      entry.textContent = Number.isFinite(value) ? `账户 ¥${value.toFixed(2)}` : '账户';
    } catch (_error) {
      entry.textContent = '账户';
    }
  };

  let scheduled = false;
  const mountServiceButtons = () => {
    if (document.querySelector('[data-olivia-service-buttons]')) return;
    const badge = Array.from(document.querySelectorAll('span,div,button')).find(node =>
      node.children.length === 0 && node.textContent.trim() === 'Resonance Edition');
    if (!badge) return;
    const group = document.createElement('div');
    group.setAttribute('data-olivia-service-buttons', '');
    group.setAttribute('aria-label', '账户与云服务');
    group.style.cssText = 'display:inline-flex;align-items:center;gap:8px;margin-right:8px;flex-shrink:0;-webkit-app-region:no-drag';
    // Cloud sync is not connected yet; the icon only marks where it will live.
    const cloud = document.createElement('span');
    cloud.setAttribute('data-olivia-cloud-soon', '');
    cloud.setAttribute('role', 'img');
    cloud.setAttribute('aria-label', '云同步即将推出');
    cloud.title = '云同步即将推出';
    cloud.style.cssText = 'display:inline-flex;align-items:center;color:#8b8d92;-webkit-app-region:no-drag';
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    for (const [name, value] of Object.entries({width: '20', height: '20', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
      'stroke-width': '1.6', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true'})) svg.setAttribute(name, value);
    const outline = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    outline.setAttribute('d', 'M7 18a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 9.5a4 4 0 0 1-.5 7.97z');
    svg.append(outline);
    cloud.append(svg);
    const entry = button('账户', () => openDialog(false, 'relay'));
    entry.setAttribute('data-olivia-account-entry', '');
    entry.style.cssText = 'font:inherit;font-size:14px;line-height:20px;padding:5px 12px;white-space:nowrap;border:1px solid #686a70;border-radius:8px;background:#242426;color:#f9fafb;cursor:pointer;-webkit-app-region:no-drag';
    entry.setAttribute('aria-haspopup', 'dialog');
    group.append(cloud, entry);
    badge.parentElement.style.display = 'flex';
    badge.parentElement.style.flexDirection = 'row';
    badge.parentElement.style.alignItems = 'center';
    badge.parentElement.style.flexWrap = 'nowrap';
    badge.style.flexShrink = '0';
    badge.before(group);
    void refreshAccountEntry();
  };
  const constrainLetterInputs = () => {
    const matches = new Set(
      Array.from(
        document.querySelectorAll('[role="dialog"], .el-dialog')
      ).filter((dialog) => {
        if (dialog.closest(`[${DIALOG_ATTR}]`)) {
          return false;
        }
        const textareas = Array.from(dialog.querySelectorAll("textarea")).filter(node=>!node.closest("[data-olivia-cover-panel]"));
        const titles = Array.from(
          dialog.querySelectorAll('h1,h2,h3,[class*="title"]')
        ).filter((item) => item.textContent.trim() === LETTER_COMPOSER_TITLE);
        const submitButtons = Array.from(
          dialog.querySelectorAll("button")
        ).filter((item) => item.textContent.trim() === LETTER_SUBMIT_LABEL);
        return textareas.length === 1 && titles.length === 1 && submitButtons.length === 1;
      }).map((dialog) => Array.from(dialog.querySelectorAll("textarea")).find(node=>!node.closest("[data-olivia-cover-panel]")))
    );
    if (matches.size !== 1) {
      return;
    }
    const input=matches.values().next().value;
    input.maxLength = LETTER_CHARACTER_LIMIT;
    mountCoverComposer(input);
  };

  const proactiveSendButton = (node) => {
    const label = (node.textContent || "").trim();
    return node.tagName === "BUTTON"
      && (label === LETTER_SUBMIT_LABEL || /寄出|发送|写信/.test(label));
  };

  const applyProactiveSendGate = () => {
    const busy = proactiveState.busy === true;
    const candidates = Array.from(document.querySelectorAll("button"))
      .filter(proactiveSendButton);
    for (const node of candidates) {
      if (!node.dataset.oliviaProactiveOriginalDisabled) {
        node.dataset.oliviaProactiveOriginalDisabled = String(node.disabled);
      }
      if (busy) {
        node.disabled = true;
        node.setAttribute("aria-disabled", "true");
      } else {
        node.disabled = node.dataset.oliviaProactiveOriginalDisabled === "true";
        node.removeAttribute("aria-disabled");
        delete node.dataset.oliviaProactiveOriginalDisabled;
      }
    }
    for (const dialog of document.querySelectorAll('[role="dialog"], .el-dialog')) {
      if (!busy) {
        dialog.querySelector('[data-olivia-proactive-writing]')?.remove();
        continue;
      }
      if (!dialog.querySelector('[data-olivia-proactive-writing]')) {
        const status = text("p", "林离正在写信", "text-text-secondary text-body-m");
        status.setAttribute("data-olivia-proactive-writing", "true");
        status.setAttribute("role", "status");
        dialog.append(status);
      }
    }
  };

  proactiveStateListeners.add(applyProactiveSendGate);

  const schedule = () => {
    if (scheduled) {
      return;
    }
    scheduled = true;
    window.requestAnimationFrame(() => {
      scheduled = false;
      installNativeWorldRoute();
      constrainLetterInputs();
      applyProactiveSendGate();
      mountMainNavigation();
      mountLocalSongEntry();
      mountShell();
      mountServiceButtons();
      maybeOpenInitialSetup();
    });
  };

  const observer = new MutationObserver(schedule);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("hashchange", schedule);
  window.addEventListener("olivia-native-router-ready", schedule);
  window.addEventListener("popstate", schedule);
  if (typeof window.setInterval === "function") {
    setupPoll = window.setInterval(maybeOpenInitialSetup, 1500);
    proactiveStatusTimer = window.setInterval(refreshProactiveStatus, 2500);
  }
  schedule();
})();
'''


BOOTSTRAP_JAVASCRIPT = r'''
(() => {
  if (!window.customElements || customElements.get('olivia-letter-audio')) return;
  const style = document.createElement('style');
  style.textContent = `
    .mail-content-body-inner>.mail-responsive-card{width:min(100%,650px);min-width:0;height:auto;min-height:0;flex:0 0 auto;aspect-ratio:auto}
    .mail-content-body-inner>.mail-responsive-card>.mail-card-content{width:100%;min-width:0;min-height:0;height:auto;aspect-ratio:16/9;box-sizing:border-box}
    olivia-letter-audio{display:block;margin:8px var(--tp-spacing-6,16px) 0;color:var(--tp-grey-0,#333);font-family:inherit}
    .mail-box-reply-content-text:has(olivia-letter-audio){height:auto;min-height:290px}
    .mail-box-reply-content-text:has(olivia-letter-audio) .mail-box-reply-content-textarea{height:180px;margin-top:10px}
    .mail-responsive-card:has(olivia-photo){height:auto!important;aspect-ratio:auto;flex:0 0 auto!important}
    .olivia-photo-stack:has(olivia-photo .olivia-letter-photo-print){position:relative;isolation:isolate;padding-bottom:44px}
    .olivia-photo-stack:has(olivia-photo .olivia-letter-photo-print)>.mail-box-reply-content{position:relative;z-index:1}
    .olivia-photo-stack:has(olivia-photo[data-open]){z-index:5}
    olivia-photo[data-moving]{z-index:3!important}
    olivia-photo{display:block;flex:none;margin:8px 20px 16px;max-width:100%;color:#bbb6ad;overflow-wrap:anywhere;font-family:system-ui,sans-serif;font-size:13px;line-height:1.6}
    olivia-photo:has(.olivia-letter-photo-print){position:absolute;right:56px;bottom:44px;z-index:0;display:block;max-width:calc(100% - 80px);margin:0;transform-origin:bottom right;transform:rotate(-12deg)}
    olivia-photo[data-open]{top:16px;bottom:auto;z-index:3;transform:none}
    .olivia-letter-photo-print{display:block;box-sizing:border-box;width:160px;max-width:100%;padding:6px;border:0;border-radius:0;background:#f3eee4;color:#514638;text-decoration:none;box-shadow:0 4px 12px #0003;cursor:zoom-in}
    .olivia-letter-photo-print img{display:block;max-width:100%;width:100%;height:176px;object-fit:cover}
    .olivia-letter-photo-print span{display:block;padding:4px 0;text-align:center;font:16px/1.5 SentyTEA,serif}
    olivia-photo:not([data-open]) .olivia-letter-photo-print span{display:none}
    olivia-photo[data-open] .olivia-letter-photo-print{width:auto;cursor:zoom-out;box-shadow:0 12px 32px #0006}
    olivia-photo[data-open] img{width:auto;height:auto;object-fit:contain;max-height:min(360px,60vh,var(--photo-open-height,360px))}
    .olivia-letter-photo-print:focus-visible{outline:2px solid #d6c3a4;outline-offset:4px}
    olivia-photo:empty{display:none}
    .tp-el-overlay:has(.video-preview-dialog){z-index:10000!important}
    olivia-letter-audio .voice-controls{position:relative;display:flex;flex-direction:column;align-items:center;gap:5px;padding:8px 0 12px;color:#514638}
    olivia-letter-audio .voice-controls>button{position:absolute;top:21px;left:calc(50% - 105px)}
    olivia-letter-audio .voice-controls[data-wave-style="ripple"]>button{left:calc(50% - 15px);top:21px;z-index:1}
    .olivia-audio-toolbar{display:flex;justify-content:flex-end;align-items:center;flex-wrap:wrap;gap:8px;padding:8px 0 12px;font:13px/1.5 "Microsoft YaHei",Arial,sans-serif;color:#eee9df}
    .olivia-wave-style{position:static;border:1px solid #64676e;border-radius:14px;background:#202124;color:#eee9df;font:13px/1.5 "Microsoft YaHei",Arial,sans-serif;padding:6px 10px;cursor:pointer;color-scheme:dark}
    .olivia-wave-style option{background:#202124;color:#eee9df;font:13px "Microsoft YaHei",Arial,sans-serif}
    olivia-letter-audio button{appearance:none;border:0;background:none;color:inherit;padding:6px;cursor:pointer;flex-shrink:0;line-height:1}
    olivia-letter-audio button:focus-visible,olivia-letter-audio input:focus-visible{outline:2px solid currentColor;outline-offset:3px}
    olivia-letter-audio svg{width:18px;height:18px;fill:currentColor;display:block}
    olivia-letter-audio input{min-width:20px;flex:1;height:3px;accent-color:var(--tp-grey-0,#333);cursor:pointer}
    olivia-letter-audio .voice-wave{position:relative;width:160px;max-width:65%;height:60px;color:#514638}
    olivia-letter-audio .voice-wave canvas{display:block;width:100%;height:60px;pointer-events:none}
    olivia-letter-audio .voice-wave input{position:absolute;left:0;bottom:-5px;width:100%;height:20px;margin:0;opacity:0;touch-action:pan-y}
    olivia-letter-audio .voice-wave:focus-within{outline:1px solid currentColor;outline-offset:3px}
    olivia-letter-audio .voice-volume-control{position:absolute;left:calc(50% + 88px);top:21px;font:12px Arial,sans-serif}
    olivia-letter-audio .voice-volume-control summary{display:flex;align-items:center;gap:4px;cursor:pointer;list-style:none;padding:6px}
    olivia-letter-audio .voice-volume-control summary::-webkit-details-marker{display:none}
    olivia-letter-audio .voice-volume-popup{position:absolute;right:0;top:100%;padding:12px;background:#f3eee4;border:1px solid #b7aa95;border-radius:8px;z-index:2}
    olivia-letter-audio .voice-volume{width:100px;flex:none;margin:0}
    olivia-letter-audio time{font-family:Arial,sans-serif;font-size:11px;font-variant-numeric:tabular-nums;white-space:nowrap}
    olivia-letter-audio .voice-status{font-size:13px;line-height:1.7}
    olivia-letter-audio .song-controls{display:flex;align-items:center;gap:12px;margin:14px 0;padding:12px 16px;border:1px solid #a48c5c66;border-radius:12px;background:#a48c5c14;color:#78613b}
    olivia-letter-audio .song-controls .song-title{font-size:13px;white-space:nowrap}
    olivia-letter-audio .song-controls input{accent-color:#a48c5c}
    olivia-letter-audio video{width:100%;max-height:260px;margin-top:12px;display:block}
  `;
  document.head.append(style);
  const icon=(button,paused)=>{const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d',paused?'M7 4v16l13-8z':'M6 4h4v16H6zm8 0h4v16h-4z');svg.append(path);button.replaceChildren(svg)};
  const safe=value=>{try{const u=new URL(value);return u.protocol==='http:'&&['localhost','127.0.0.1'].includes(u.hostname)&&!u.username&&!u.password&&!u.search&&!u.hash&&/^\/toy\/media\/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(wav|mp4)$/.test(u.pathname)?u.href:''}catch{return ''}};
  let current=null;
  function letterWave(wrap,seek,audio,url){
    const canvas=document.createElement('canvas');canvas.setAttribute('aria-hidden','true');wrap.append(canvas,seek);
    const reduced=matchMedia('(prefers-reduced-motion: reduce)');
    let frame=0,disposed=false,context=null,analyser=null,source=null,samples=null,kind='bars';
    const levels=new Float32Array(11);
    const start=()=>{
      if(disposed)return;
      try{
        const AudioContext=window.AudioContext||window.webkitAudioContext;
        if(!context&&AudioContext){
          context=new AudioContext();analyser=context.createAnalyser();analyser.fftSize=2048;
          samples=new Uint8Array(analyser.fftSize);samples.fill(128);source=context.createMediaElementSource(audio);
          source.connect(analyser);analyser.connect(context.destination);
        }
        context?.resume().catch(()=>{});
      }catch(_){source?.connect(context.destination)}
    };
    const draw=()=>{
      cancelAnimationFrame(frame);frame=0;if(disposed)return;
      const width=Math.max(1,wrap.clientWidth),ratio=window.devicePixelRatio||1;
      if(canvas.width!==Math.round(width*ratio)||canvas.height!==Math.round(60*ratio)){canvas.width=Math.round(width*ratio);canvas.height=Math.round(60*ratio)}
      const ctx=canvas.getContext('2d');if(!ctx)return;ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,width,60);
      const progress=audio.duration?Math.min(1,audio.currentTime/audio.duration):0;
      const moving=!audio.paused&&!audio.ended&&!document.hidden&&!reduced.matches;
      ctx.lineWidth=1.5;ctx.lineCap='round';
      if(analyser&&moving)analyser.getByteTimeDomainData(samples);
      for(let i=0;i<levels.length;i++){
        let sum=0,n=0;if(samples)for(let j=Math.floor(i*samples.length/11);j<Math.floor((i+1)*samples.length/11);j++){sum+=((samples[j]-128)/128)**2;n++}
        const target=audio.ended||reduced.matches?0:Math.min(1,Math.sqrt(sum/Math.max(1,n))*5);
        if(moving)levels[i]+=(target-levels[i])*(target>levels[i]?.65:.18);else if(audio.ended||reduced.matches)levels[i]=0;
      }
      const energy=levels.reduce((a,b)=>a+b,0)/11,center=width/2;
      if(kind==='bars'||kind==='dots'){
        for(let i=0;i<11;i++){
          const edge=1-Math.abs(i-5)/8,x=center+(i-5)*7,h=1.5+levels[i]*16*edge;
          ctx.strokeStyle=`rgba(81,70,56,${.35+edge*.55})`;ctx.lineWidth=kind==='dots'?3+levels[i]*2:2.5;
          const y=kind==='dots'?26-levels[i]*10*edge:26;
          ctx.beginPath();ctx.moveTo(x,y-(kind==='dots'?.5:h));ctx.lineTo(x,y+(kind==='dots'?.5:h));ctx.stroke();
        }
      }else{
        for(let layer=0;layer<3;layer++){
          ctx.lineWidth=1.2;ctx.strokeStyle=`rgba(81,70,56,${.75-layer*.23})`;ctx.beginPath();
          for(let i=0;i<=80;i++){
            const t=i/80;let x,y;
            if(kind==='ripple'){
              const angle=t*Math.PI*2,r=15+layer*4+energy*3+Math.sin(angle*3+audio.currentTime*2+layer)*energy*2;
              x=center+Math.cos(angle)*r;y=26+Math.sin(angle)*r;
            }else{
              x=center+(t-.5)*100;y=26+Math.sin(t*Math.PI*4+audio.currentTime*5+layer*.6)*Math.sin(t*Math.PI)*(2+energy*12)*(1-layer*.18);
            }
            if(i)ctx.lineTo(x,y);else ctx.moveTo(x,y);
          }ctx.stroke();
        }
      }
      ctx.lineWidth=1;ctx.strokeStyle='rgba(81,70,56,.25)';ctx.beginPath();ctx.moveTo(0,59);ctx.lineTo(width,59);ctx.stroke();
      ctx.strokeStyle='#514638';ctx.beginPath();ctx.moveTo(0,59);ctx.lineTo(width*progress,59);ctx.stroke();
      if(moving)frame=requestAnimationFrame(draw);
    };
    const events=['play','pause','ended','timeupdate','seeked','loadedmetadata'];events.forEach(name=>audio.addEventListener(name,draw));
    document.addEventListener('visibilitychange',draw);const resize=new ResizeObserver(draw);resize.observe(wrap);
    draw();const cleanup=()=>{disposed=true;cancelAnimationFrame(frame);resize.disconnect();events.forEach(name=>audio.removeEventListener(name,draw));document.removeEventListener('visibilitychange',draw);source?.disconnect();analyser?.disconnect();if(context&&context.state!=='closed')context.close().catch(()=>{})};
    cleanup.start=start;cleanup.setStyle=value=>{kind=['bars','dots','ribbon','ripple'].includes(value)?value:'bars';draw()};return cleanup;
  }
  window.__oliviaLetterWave=letterWave;
  const coverApi=document.currentScript?.dataset?.apiBase;
  class LetterAudio extends HTMLElement {
    static get observedAttributes(){return ['audio-url','audio-status','song-url','cover-id']}
    connectedCallback(){this.render();this.coverTimer=setInterval(()=>this.coverProgress(),4000);this.coverProgress()}
    disconnectedCallback(){clearInterval(this.coverTimer);this.styleCleanup?.();this.waveCleanup?.();this.key=null;this.audio?.pause();this.video?.pause();if(current===this.audio||current===this.video)current=null}
    attributeChangedCallback(){if(this.isConnected)this.render()}
    async coverProgress(){
      const id=this.getAttribute('cover-id');if(!id||!coverApi||this.coverBusy)return;
      this.coverBusy=true;
      try{
        const endpoint=new URL('/toy/media/progress',coverApi);endpoint.searchParams.set('letter_id',id);
        const response=await fetch(endpoint,{cache:'no-store',credentials:'omit'});if(!response.ok)return;
        const data=(await response.json()).data;const status=this.querySelector('.voice-status');if(!status||!data)return;
        const labels={loading:'正在准备翻唱…',transcribing:'正在识别原曲歌词…',loading_model:'正在加载翻唱模型…',generating:'林离正在翻唱…',decoding:'正在保存歌曲音频…',completed:'歌曲已完成，正在准备回信…'};
        const errors={COVER_LYRICS_REQUIRED:'未能识别歌词，请补充原曲歌词后重新寄信。',COVER_RUNTIME_UNAVAILABLE:'翻唱组件尚未准备完整，请检查本地组件。',COVER_GENERATION_TIMEOUT:'这次翻唱等待超时，可以手动重试。',COVER_SOURCE_REQUIRED:'这封信缺少原曲音频，请重新选择后寄信。'};
        const cloudErrors={GPU_TLS_FAILED:'云端证书校验失败，请更新补丁并检查电脑时间。',GPU_CONNECTION_TIMEOUT:'云端连接超时，本次生成已停止等待。',GPU_CONNECT_FAILED:'无法连接云端，本次生成未完成。',GPU_CONNECTION_FAILED:'云端连接中断，本次生成未完成。',GPU_AUTH_FAILED:'云端 Key 验证失败，请检查 Olivia 账户。',GPU_QUEUE_FULL:'云端队列已满，本次任务未进入队列。',GPU_TASK_TIMEOUT:'云端任务等待超时，已停止等待。',GPU_TASK_FAILED:'云端生成失败。',GPU_DOWNLOAD_FAILED:'生成结果下载失败。',GPU_SHARED_SCENE_MISSING:'视频素材与云端不匹配，请联系管理员。',MEDIA_JOB_INTERRUPTED:'上次生成已中断，未自动重复提交。'};
        cloudErrors.GPU_INSUFFICIENT_BALANCE='Olivia 可用余额不足，本次媒体任务未入队。请充值后重试。';
        cloudErrors.GPU_BILLING_CONSENT_REQUIRED='请更新收费版客户端，确认费用上限后再生成。';
        if(['FAILED','UNAVAILABLE'].includes(data.status)) {
          const code=typeof data.error_code==='string'&&/^[A-Z][A-Z0-9_]{0,95}$/.test(data.error_code)?data.error_code:'';
          status.textContent=(cloudErrors[code]||errors[code]||'本次媒体生成未完成。')+' 文字回信已保留。'+(code?`（${code}）`:'');
          clearInterval(this.coverTimer);
        }
        else if(data.status!=='COMPLETED'&&labels[data.stage])status.textContent=labels[data.stage];
      }catch(_){}finally{this.coverBusy=false}
    }
    render(){
      const url=safe(this.getAttribute('audio-url')||''), song=safe(this.getAttribute('song-url')||''), state=this.getAttribute('audio-status')||'';
      const key=JSON.stringify([url,song,state]);if(this.key===key)return;
      this.key=key;const sameAudio=this.audio?.src===url,oldTime=sameAudio?this.audio.currentTime:0,wasPlaying=sameAudio&&!this.audio.paused;
      this.styleCleanup?.();this.waveCleanup?.();this.audio?.pause();this.video?.pause();this.replaceChildren();this.audio=null;this.video=null;
      const status=document.createElement('div');status.className='voice-status';status.setAttribute('role','status');
      if(url){
        const audio=new Audio();audio.crossOrigin='anonymous';audio.src=url;this.audio=audio;audio.preload='metadata';
        let savedVolume=100;try{savedVolume=Number(localStorage.getItem('olivia.letter.voice-volume')??100)}catch(_){};
        audio.volume=Number.isFinite(savedVolume)?Math.max(0,Math.min(100,savedVolume))/100:1;
        const row=document.createElement('div');row.className='voice-controls';
        const button=document.createElement('button');button.type='button';icon(button,true);button.setAttribute('aria-label','播放语音');
        const seek=document.createElement('input');seek.type='range';seek.min='0';seek.max='0';seek.step='0.1';seek.value='0';seek.setAttribute('aria-label','语音播放进度');
        const stamp=document.createElement('time');stamp.textContent='0:00';
        const fmt=x=>Math.floor(x/60)+':'+String(Math.floor(x%60)).padStart(2,'0');
        const sync=()=>{icon(button,audio.paused);button.setAttribute('aria-label',audio.paused?'播放语音':'暂停语音');seek.max=String(audio.duration||0);seek.value=String(audio.currentTime);stamp.textContent=fmt(audio.currentTime)+' / '+fmt(audio.duration||0)};
        button.onclick=()=>{this.waveCleanup?.start();if(audio.paused)audio.play().catch(()=>{status.textContent='语音暂时无法播放，请稍后重新打开信件。'});else audio.pause()};
        seek.oninput=()=>{audio.currentTime=Number(seek.value)};
        audio.onplay=()=>{if(current&&current!==audio)current.pause();document.querySelectorAll('video').forEach(v=>v.pause());current=audio;sync()};audio.onpause=sync;audio.ontimeupdate=sync;
        audio.onloadedmetadata=()=>{audio.currentTime=Math.min(oldTime,audio.duration||0);if(state==='COMPLETED')status.textContent='';sync();if(wasPlaying)audio.play().catch(()=>{})};
        audio.onerror=()=>{status.textContent='语音暂时无法播放，请稍后重新打开信件。'};
        audio.onended=sync;
        const wave=document.createElement('div');wave.className='voice-wave';
        const volume=document.createElement('input');volume.type='range';volume.className='voice-volume';volume.min='0';volume.max='100';volume.step='1';volume.value=String(Math.round(audio.volume*100));volume.setAttribute('aria-label','语音音量');
        volume.oninput=()=>{audio.volume=Number(volume.value)/100;try{localStorage.setItem('olivia.letter.voice-volume',volume.value)}catch(_){}};
        const volumeControl=document.createElement('details');volumeControl.className='voice-volume-control';
        const volumeToggle=document.createElement('summary');volumeToggle.setAttribute('aria-label','调节语音音量');const speaker=document.createElementNS('http://www.w3.org/2000/svg','svg');speaker.setAttribute('viewBox','0 0 24 24');speaker.setAttribute('aria-hidden','true');speaker.style.fill='none';speaker.style.stroke='currentColor';speaker.style.strokeWidth='1.6';const speakerPath=document.createElementNS('http://www.w3.org/2000/svg','path');speakerPath.setAttribute('d','M11 5 6 9H3v6l5 4zM15 8a6 6 0 0 1 0 8M18 5a10 10 0 0 1 0 14');speaker.append(speakerPath);const volumeLabel=document.createElement('span');volumeLabel.textContent='音量';volumeToggle.append(speaker,volumeLabel);
        const volumePopup=document.createElement('div');volumePopup.className='voice-volume-popup';volumePopup.append(volume);volumeControl.append(volumeToggle,volumePopup);
        volumeControl.addEventListener('keydown',event=>{if(event.key==='Escape'){volumeControl.open=false;volumeToggle.focus()}});
        row.append(button,wave,stamp,volumeControl);this.append(row);this.waveCleanup=letterWave(wave,seek,audio,url);
        const styles=document.createElement('select');styles.className='olivia-wave-style';styles.setAttribute('aria-label','波形样式');styles.title='选择波形样式';
        for(const [value,label] of [['bars','淡墨呼吸'],['dots','浮动墨点'],['ribbon','轻柔声带'],['ripple','声音涟漪']]){const option=document.createElement('option');option.value=value;option.textContent=label;styles.append(option)}
        try{styles.value=localStorage.getItem('olivia.letter.wave-style')||'bars'}catch(_){}if(!styles.value)styles.value='bars';
        const choose=()=>{row.dataset.waveStyle=styles.value;this.waveCleanup.setStyle(styles.value)};
        styles.onchange=()=>{choose();try{localStorage.setItem('olivia.letter.wave-style',styles.value)}catch(_){}};choose();
        const collect=document.createElement('button');collect.type='button';collect.className='olivia-wave-style';collect.textContent='添加到曲库';
        collect.onclick=async()=>{if(collect.disabled)return;collect.disabled=true;
          try {const response=await fetch(new URL('/toy/local-songs/from-letter',coverApi),{method:'POST',credentials:'omit',
            headers:{'Content-Type':'application/json','X-Olivia-Companion-Action':'confirmed'},body:JSON.stringify({letter_id:this.getAttribute('cover-id')})});
            const result=await response.json();if(!response.ok||result.code!==0)throw Error();collect.textContent='已添加到曲库';window.dispatchEvent(new Event('olivia-local-catalog-ready'));
          }catch(_){collect.textContent='添加失败，点击重试';collect.disabled=false;}};
        const toolbar=document.createElement('div');toolbar.className='olivia-audio-toolbar';toolbar.setAttribute('aria-label','信件语音工具');toolbar.append(styles);
        if(this.getAttribute('cover-id')&&state==='COMPLETED')toolbar.append(collect);
        toolbar.setAttribute('data-html2canvas-ignore','true');
        const placeToolbar=()=>{
          if(!this.isConnected)return;
          const paper=this.closest('.mail-box-reply-content');
          if(paper)paper.before(toolbar);else this.before(toolbar);
        };
        placeToolbar();
        // Vue applies the text-paper class after child custom elements mount.
        // Re-anchor after that render instead of leaving controls inside paper.
        const placementFrame=requestAnimationFrame(placeToolbar);
        this.styleCleanup=()=>{cancelAnimationFrame(placementFrame);toolbar.remove()};
      }
      if(!url)status.textContent=['FAILED','UNAVAILABLE'].includes(state)?'这次声音或视频未能生成，文字回信已保留。':'林离正在准备回信音频…';
      else if(['FAILED','UNAVAILABLE'].includes(state))status.textContent='歌曲暂时未完成，语音可以先听。';
      else if(state!=='COMPLETED')status.textContent='语音已录好，歌曲制作中…';
      this.append(status);
      if(song){
        const music=new Audio(song);music.preload='metadata';this.video=music;
        const row=document.createElement('div');row.className='song-controls';
        const title=document.createElement('span');title.className='song-title';title.textContent='♫ 为你写的歌';
        const button=document.createElement('button');button.type='button';icon(button,true);
        const seek=document.createElement('input');seek.type='range';seek.min='0';seek.max='0';seek.step='0.1';seek.value='0';seek.setAttribute('aria-label','歌曲播放进度');
        const stamp=document.createElement('time');
        const fmt=x=>Math.floor(x/60)+':'+String(Math.floor(x%60)).padStart(2,'0');
        const sync=()=>{icon(button,music.paused);button.setAttribute('aria-label',music.paused?'播放歌曲':'暂停歌曲');seek.max=String(music.duration||0);seek.value=String(music.currentTime);stamp.textContent=fmt(music.currentTime)+' / '+fmt(music.duration||0)};
        button.onclick=()=>{if(music.paused)music.play().catch(()=>{status.textContent='歌曲暂时无法播放，请重新打开信件。'});else music.pause()};
        seek.oninput=()=>{music.currentTime=Number(seek.value)};
        music.onplay=()=>{if(current&&current!==music)current.pause();document.querySelectorAll('video').forEach(v=>v.pause());current=music;sync()};
        music.onpause=sync;music.ontimeupdate=sync;music.onloadedmetadata=sync;music.onended=sync;sync();
        row.append(button,title,seek,stamp);this.append(row);
      }
    }
  }
  customElements.define('olivia-letter-audio',LetterAudio);
})();
''' + BOOTSTRAP_JAVASCRIPT

BOOTSTRAP_JAVASCRIPT = BOOTSTRAP_JAVASCRIPT.replace(
    "__OLIVIA_WECHAT_PAYMENT_QR__",
    "data:image/jpeg;base64," + base64.b64encode(
        (Path(__file__).parent / "installer/assets/wechat-payment.jpeg").read_bytes()
    ).decode("ascii"),
)

__all__ = ["BOOTSTRAP_JAVASCRIPT", "SETTINGS_UI_VERSION"]
