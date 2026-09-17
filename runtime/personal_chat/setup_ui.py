"""Small settings-page extension for QQ/Weixin personal-chat binding."""

PERSONAL_CHAT_SETUP_JAVASCRIPT = r'''(() => {
  "use strict";

  const script = document.currentScript;
  const apiBase = script?.dataset?.apiBase;
  if (!apiBase || !document.createElement) return;

  const STATUS = "/toy/personal-chat/setup/status";
  const WECHAT_START = "/toy/personal-chat/setup/wechat/start";
  const WECHAT_VERIFY = "/toy/personal-chat/setup/wechat/verify";
  const QQ_CONFIGURE = "/toy/personal-chat/setup/qq/configure";
  const CONFIRM_HEADER = "X-Olivia-Companion-Action";
  const CONFIRM_VALUE = "confirmed";
  let pollTimer = null;

  const node = (tag, value, className) => {
    const element = document.createElement(tag);
    if (value !== undefined && value !== null) element.textContent = String(value);
    if (className) element.className = className;
    return element;
  };

  const request = async (path, options = {}) => {
    const headers = new Headers(options.headers || {});
    headers.set(CONFIRM_HEADER, CONFIRM_VALUE);
    if (options.body !== undefined) headers.set("Content-Type", "application/json");
    const response = await fetch(new URL(path, apiBase), {
      method: options.method || "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      cache: "no-store",
      credentials: "omit",
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) {}
    if (!response.ok) {
      const error = new Error(payload.error || `HTTP_${response.status}`);
      error.code = payload.error || `HTTP_${response.status}`;
      throw error;
    }
    return payload;
  };

  const stateLabel = (value) => ({
    IDLE: "尚未设置",
    STARTING: "正在获取二维码…",
    SCAN_REQUIRED: "等待微信扫码",
    SCANNED: "已扫码，请在手机上确认",
    VERIFY_REQUIRED: "需要输入微信验证码",
    TESTING: "正在检查连接…",
    READY_RESTART: "绑定成功，重启 Olivia 后生效",
    LISTENING: "已连接",
    RECONNECTING: "正在重连",
    SETUP_REQUIRED: "需要设置",
    CONFIGURED_RESTART: "已保存，重启 Olivia 后生效",
    FAILED: "连接失败",
  }[value] || value || "未知状态");

  const input = (placeholder, type = "text") => {
    const element = document.createElement("input");
    element.type = type;
    element.placeholder = placeholder;
    element.autocomplete = "off";
    element.className = "olivia-chat-input";
    return element;
  };

  const action = (label, handler) => {
    const button = node("button", label, "olivia-chat-action");
    button.type = "button";
    button.addEventListener("click", async () => {
      if (button.disabled) return;
      button.disabled = true;
      try { await handler(button); }
      finally { button.disabled = false; }
    });
    return button;
  };

  const ensureStyle = () => {
    if (document.querySelector("style[data-olivia-personal-chat-style]")) return;
    const style = document.createElement("style");
    style.dataset.oliviaPersonalChatStyle = "true";
    style.textContent = `
      [data-olivia-personal-chat-setup]{margin-top:18px;padding-top:18px;border-top:1px solid rgba(255,255,255,.09)}
      [data-olivia-personal-chat-setup] .olivia-chat-title{font-size:16px;font-weight:600;color:var(--color-text-body,#eee)}
      [data-olivia-personal-chat-setup] .olivia-chat-copy{font-size:13px;line-height:1.7;color:var(--color-text-secondary,#aaa);margin-top:6px}
      [data-olivia-personal-chat-setup] .olivia-chat-channel{margin-top:14px;padding:14px;border:1px solid rgba(255,255,255,.10);border-radius:10px;background:rgba(255,255,255,.025)}
      [data-olivia-personal-chat-setup] .olivia-chat-row{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
      [data-olivia-personal-chat-setup] .olivia-chat-name{font-size:14px;font-weight:600;color:var(--color-text-body,#eee)}
      [data-olivia-personal-chat-setup] .olivia-chat-state{font-size:12px;color:var(--color-text-secondary,#aaa)}
      [data-olivia-personal-chat-setup] .olivia-chat-action{border:1px solid rgba(255,255,255,.18);border-radius:8px;padding:7px 12px;background:rgba(255,255,255,.07);color:inherit;cursor:pointer}
      [data-olivia-personal-chat-setup] .olivia-chat-action:disabled{opacity:.45;cursor:default}
      [data-olivia-personal-chat-setup] .olivia-chat-fields{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
      [data-olivia-personal-chat-setup] .olivia-chat-input{min-width:0;border:1px solid rgba(255,255,255,.14);border-radius:8px;padding:8px 10px;background:rgba(0,0,0,.18);color:inherit;outline:none}
      [data-olivia-personal-chat-setup] .olivia-chat-wide{grid-column:1/-1}
      [data-olivia-personal-chat-setup] .olivia-chat-qr{display:block;width:210px;height:210px;object-fit:contain;background:#fff;border-radius:8px;padding:8px;margin:12px auto 0}
      [data-olivia-personal-chat-setup] .olivia-chat-error{font-size:12px;color:#d9a2a2;margin-top:8px;white-space:pre-wrap}
      [data-olivia-personal-chat-setup] .olivia-chat-verify{display:flex;gap:8px;margin-top:10px}
      @media (max-width:650px){[data-olivia-personal-chat-setup] .olivia-chat-fields{grid-template-columns:1fr}}
    `;
    document.head?.append(style);
  };

  const mount = () => {
    if (document.querySelector("[data-olivia-personal-chat-setup]")) return true;
    const anchor = document.querySelector("[data-olivia-proactive-settings]");
    if (!anchor || !anchor.parentNode) return false;
    ensureStyle();

    const root = document.createElement("section");
    root.dataset.oliviaPersonalChatSetup = "true";
    const title = node("div", "QQ / 微信聊天", "olivia-chat-title");
    const copy = node("div", "当林离提出交换联系方式、并且你选择 QQ 或微信后，可以在这里完成绑定。微信可直接扫码；QQ 需要本机已运行 NapCat OneBot。", "olivia-chat-copy");
    const content = document.createElement("div");
    root.append(title, copy, content);
    anchor.after(root);

    const renderError = (parent, error) => {
      let target = parent.querySelector(".olivia-chat-error");
      if (!target) {
        target = node("div", "", "olivia-chat-error");
        parent.append(target);
      }
      target.textContent = error?.code || error?.message || String(error || "连接失败");
    };

    const renderWechat = (status) => {
      const box = node("div", null, "olivia-chat-channel");
      const top = node("div", null, "olivia-chat-row");
      const left = document.createElement("div");
      const listener = status.listeners?.wechat;
      const state = listener === "LISTENING" ? "LISTENING" : status.wechat?.state || listener;
      left.append(node("div", "微信", "olivia-chat-name"), node("div", stateLabel(state), "olivia-chat-state"));
      const start = action(status.configured?.wechat ? "重新绑定" : "生成二维码", async () => {
        try {
          await request(WECHAT_START, {method: "POST", body: {}});
          await refresh(true);
        } catch (error) { renderError(box, error); }
      });
      top.append(left, start);
      box.append(top);

      if (status.wechat?.qr_data && ["SCAN_REQUIRED", "SCANNED", "VERIFY_REQUIRED"].includes(status.wechat.state)) {
        const image = document.createElement("img");
        image.className = "olivia-chat-qr";
        image.alt = "微信绑定二维码";
        image.src = status.wechat.qr_data;
        box.append(image);
      }
      if (status.wechat?.state === "VERIFY_REQUIRED") {
        const row = node("div", null, "olivia-chat-verify");
        const code = input("手机上显示的验证码");
        code.inputMode = "numeric";
        code.maxLength = 8;
        const verify = action("提交验证码", async () => {
          try {
            await request(WECHAT_VERIFY, {method: "POST", body: {code: code.value.trim()}});
            await refresh(true);
          } catch (error) { renderError(box, error); }
        });
        row.append(code, verify);
        box.append(row);
      }
      if (status.wechat?.error) renderError(box, {code: status.wechat.error});
      return box;
    };

    const renderQQ = (status) => {
      const box = node("div", null, "olivia-chat-channel");
      const top = node("div", null, "olivia-chat-row");
      const left = document.createElement("div");
      const listener = status.listeners?.qq;
      const state = listener === "LISTENING" ? "LISTENING" : status.qq?.state || listener;
      left.append(node("div", "QQ（实验功能）", "olivia-chat-name"), node("div", stateLabel(state), "olivia-chat-state"));
      top.append(left);
      box.append(top);
      box.append(node("div", "先在本机启动 NapCat，并开启 OneBot WebSocket。林离使用一个独立 QQ，你自己的 QQ 作为唯一 owner。Token 只会用 Windows DPAPI 加密保存在本机。", "olivia-chat-copy"));

      const fields = node("div", null, "olivia-chat-fields");
      const account = input("林离的 QQ 号");
      const owner = input("你的 QQ 号");
      const url = input("OneBot 地址，例如 ws://127.0.0.1:3001");
      url.value = "ws://127.0.0.1:3001";
      url.classList.add("olivia-chat-wide");
      const token = input("OneBot Token（至少 16 位）", "password");
      token.classList.add("olivia-chat-wide");
      fields.append(account, owner, url, token);
      box.append(fields);
      const controls = node("div", null, "olivia-chat-row");
      controls.style.marginTop = "10px";
      controls.append(action("测试并保存", async () => {
        try {
          await request(QQ_CONFIGURE, {method: "POST", body: {
            account: account.value.trim(), owner: owner.value.trim(), url: url.value.trim(), token: token.value,
          }});
          token.value = "";
          await refresh(false);
        } catch (error) { token.value = ""; renderError(box, error); }
      }));
      box.append(controls);
      if (status.qq?.error) renderError(box, {code: status.qq.error});
      return box;
    };

    const schedule = (status) => {
      if (pollTimer !== null) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
      if (["STARTING", "SCAN_REQUIRED", "SCANNED", "VERIFY_REQUIRED"].includes(status.wechat?.state)) {
        pollTimer = setTimeout(() => refresh(true), 2000);
      }
    };

    async function refresh(silent = false) {
      try {
        const status = await request(STATUS);
        const selected = Array.isArray(status.selected_channels) ? status.selected_channels : [];
        const fragment = document.createDocumentFragment();
        if (!selected.length) {
          fragment.append(node("div", "还没有选择聊天渠道。等林离主动提出交换联系方式、你回复 QQ 或微信后，这里会自动出现绑定入口。", "olivia-chat-copy"));
        } else {
          if (selected.includes("wechat")) fragment.append(renderWechat(status));
          if (selected.includes("qq")) fragment.append(renderQQ(status));
        }
        content.replaceChildren(fragment);
        schedule(status);
      } catch (error) {
        if (!silent) {
          content.replaceChildren(node("div", error?.code || error?.message || "读取聊天设置失败", "olivia-chat-error"));
        }
      }
    }

    refresh(false);
    return true;
  };

  if (!mount()) {
    const observer = new MutationObserver(() => {
      if (mount()) observer.disconnect();
    });
    observer.observe(document.documentElement, {childList: true, subtree: true});
  }
})();
'''


__all__ = ["PERSONAL_CHAT_SETUP_JAVASCRIPT"]
