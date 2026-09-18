"""Small settings-page extension for QQ/Weixin personal-chat binding."""

PERSONAL_CHAT_SETUP_JAVASCRIPT = r'''(() => {
  "use strict";

  const script = document.currentScript;
  const apiBase = script?.dataset?.apiBase;
  if (!apiBase || !document.createElement) return;

  const STATUS = "/toy/personal-chat/setup/status";
  const CHANNEL_CHOICE = "/toy/personal-chat/setup/channel-choice";
  const WECHAT_START = "/toy/personal-chat/setup/wechat/start";
  const WECHAT_VERIFY = "/toy/personal-chat/setup/wechat/verify";
  const QQ_CONFIGURE = "/toy/personal-chat/setup/qq/configure";
  const NAPCAT_INSTALL = "/toy/personal-chat/setup/qq/napcat/install";
  const NAPCAT_START = "/toy/personal-chat/setup/qq/napcat/start";
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
    STARTING: "正在启动…",
    SCAN_REQUIRED: "等待微信扫码",
    SCANNED: "已扫码，请在手机上确认",
    VERIFY_REQUIRED: "需要输入微信验证码",
    TESTING: "正在检查连接…",
    READY_RESTART: "绑定成功，重启 Olivia 后生效",
    CONNECTING: "正在连接",
    CONNECTED: "已连接",
    LISTENING: "已连接",
    RECONNECTING: "正在重连",
    AUTH_REQUIRED: "登录已失效，需要重新绑定",
    SETUP_REQUIRED: "需要设置",
    CONFIGURED_RESTART: "已保存，重启 Olivia 后生效",
    DOWNLOADING: "正在下载 QQ 组件…",
    INSTALLER_READY: "安装器已准备",
    INSTALLER_OPENED: "安装窗口已打开",
    READY: "QQ 组件已安装",
    AWAITING_QQ_LOGIN: "等待 QQ 登录",
    ONEBOT_PROBING: "QQ 已登录，正在检查 OneBot",
    ONEBOT_CONFIG_PENDING: "QQ 已登录，正在加载 OneBot 配置",
    ONEBOT_READY: "QQ 已登录，OneBot 已就绪",
    RUNNING: "QQ 组件正在运行",
    E2E_VERIFIED: "端到端已验证",
    PLATFORM_ACCEPTED: "平台已接受回复",
    PLATFORM_CONFIRMED: "平台已确认回复",
    UNCONFIRMED: "回复投递未确认",
    UNSUPPORTED: "当前系统不支持",
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
      [data-olivia-personal-chat-setup] .olivia-chat-subcard{margin-top:12px;padding:12px;border:1px solid rgba(255,255,255,.08);border-radius:8px}
      [data-olivia-personal-chat-setup] .olivia-chat-row{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
      [data-olivia-personal-chat-setup] .olivia-chat-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
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
      [data-olivia-personal-chat-setup] details{margin-top:12px}
      [data-olivia-personal-chat-setup] summary{cursor:pointer;color:var(--color-text-secondary,#aaa);font-size:12px}
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
    const copy = node("div", "收到林离的联系方式邀请后，可以直接在这里选择并绑定。微信直接扫码；QQ 可以由 Olivia 一键准备本地 QQ 组件，不需要手填 OneBot 参数。", "olivia-chat-copy");
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

    const chooseChannel = async (choice, parent) => {
      try {
        await request(CHANNEL_CHOICE, {method: "POST", body: {choice}});
        if (choice === "wechat" || choice === "both") {
          await request(WECHAT_START, {method: "POST", body: {}});
        }
        await refresh(true);
      } catch (error) { renderError(parent, error); }
    };

    const renderChannelChoice = () => {
      const box = node("div", null, "olivia-chat-channel");
      box.append(node("div", "已经收到联系方式邀请", "olivia-chat-name"));
      box.append(node("div", "不用再寄一封信确认。直接选择你想使用的聊天方式；选择微信会立即打开扫码绑定。", "olivia-chat-copy"));
      const actions = node("div", null, "olivia-chat-actions");
      actions.append(
        action("微信", async () => chooseChannel("wechat", box)),
        action("QQ", async () => chooseChannel("qq", box)),
        action("两个都要", async () => chooseChannel("both", box)),
      );
      box.append(actions);
      return box;
    };

    const renderWechat = (status) => {
      const box = node("div", null, "olivia-chat-channel");
      const top = node("div", null, "olivia-chat-row");
      const left = document.createElement("div");
      const listener = status.listeners?.wechat;
      const setupState = status.wechat?.state;
      const state = setupState && setupState !== "IDLE" ? setupState : listener;
      left.append(node("div", "微信", "olivia-chat-name"), node("div", stateLabel(state), "olivia-chat-state"));
      const start = action(status.configured?.wechat ? "重新绑定" : "生成二维码", async () => {
        try {
          await request(WECHAT_START, {method: "POST", body: {}});
          await refresh(true);
        } catch (error) { renderError(box, error); }
      });
      top.append(left, start);
      box.append(top);
      if (status.e2e_verified_at?.wechat) {
        box.append(node("div", "端到端连接已验证：微信客户端确实收到了 Olivia 的测试回复。", "olivia-chat-copy"));
      } else if (listener === "CONNECTED") {
        const pending = (status.connection_test_pending || []).includes("wechat");
        box.append(node("div",
          pending
            ? "连接测试进行中：请把微信里收到的 4 位验证码原样回复。"
            : "接收链路正常。请在微信里发送 /连接测试；收到 4 位验证码后原样回复，完成真实端到端验证。",
          "olivia-chat-copy"));
      }
      if (status.delivery_health?.wechat === "UNCONFIRMED") {
        box.append(node("div", "最近一次微信回复只得到表面成功响应，客户端投递尚未确认；Olivia 不会自动重发，避免重复消息。", "olivia-chat-error"));
      }

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

    const renderAdvancedQQ = (box) => {
      const details = document.createElement("details");
      const summary = node("summary", "高级：连接已有 NapCat / OneBot");
      details.append(summary);
      const fields = node("div", null, "olivia-chat-fields");
      const account = input("林离的 QQ 号");
      const owner = input("你的 QQ 号");
      const url = input("OneBot 地址，例如 ws://127.0.0.1:3001");
      url.value = "ws://127.0.0.1:3001";
      url.classList.add("olivia-chat-wide");
      const token = input("OneBot Token（至少 16 位）", "password");
      token.classList.add("olivia-chat-wide");
      fields.append(account, owner, url, token);
      details.append(fields);
      const controls = node("div", null, "olivia-chat-actions");
      controls.append(action("测试并保存", async () => {
        try {
          await request(QQ_CONFIGURE, {method: "POST", body: {
            managed: false,
            account: account.value.trim(), owner: owner.value.trim(), url: url.value.trim(), token: token.value,
          }});
          token.value = "";
          await refresh(false);
        } catch (error) { token.value = ""; renderError(box, error); }
      }));
      details.append(controls);
      return details;
    };

    const renderQQ = (status) => {
      const box = node("div", null, "olivia-chat-channel");
      const top = node("div", null, "olivia-chat-row");
      const left = document.createElement("div");
      const listener = status.listeners?.qq;
      const setupState = status.qq?.state;
      const state = setupState && setupState !== "IDLE" ? setupState : listener;
      left.append(node("div", "QQ（实验功能）", "olivia-chat-name"), node("div", stateLabel(state), "olivia-chat-state"));
      top.append(left);
      box.append(top);

      const napcat = status.napcat || {};
      const component = node("div", null, "olivia-chat-subcard");
      const componentTop = node("div", null, "olivia-chat-row");
      const componentLeft = document.createElement("div");
      componentLeft.append(
        node("div", "QQ 本地组件", "olivia-chat-name"),
        node("div", `${stateLabel(napcat.state)} · ${napcat.version || ""}`, "olivia-chat-state"),
      );
      componentTop.append(componentLeft);
      if (!napcat.installed && !["DOWNLOADING", "INSTALLER_OPENED"].includes(napcat.state)) {
        componentTop.append(action("一键安装", async () => {
          try {
            await request(NAPCAT_INSTALL, {method: "POST", body: {}});
            await refresh(true);
          } catch (error) { renderError(component, error); }
        }));
      } else if (napcat.installed && napcat.state !== "ONEBOT_READY") {
        const label = napcat.state === "AWAITING_QQ_LOGIN" ? "重新打开 QQ 登录" : "启动并登录 QQ";
        componentTop.append(action(label, async () => {
          try {
            await request(NAPCAT_START, {method: "POST", body: {}});
            await refresh(true);
          } catch (error) { renderError(component, error); }
        }));
      }
      component.append(componentTop);
      if (napcat.state === "DOWNLOADING") {
        component.append(node("div", "正在获取并校验 NapCat 发布的固定 Node 运行包。", "olivia-chat-copy"));
      } else if (napcat.state === "AWAITING_QQ_LOGIN") {
        component.append(node("div", "NapCat WebUI 已真实启动。请在本机登录页完成 QQ 登录；Olivia 会自动等待 OneBot 就绪。", "olivia-chat-copy"));
      } else if (napcat.state === "ONEBOT_PROBING") {
        component.append(node("div", "已经检测到 OneBot 端口，正在确认真实登录账号。", "olivia-chat-copy"));
      } else if (napcat.state === "ONEBOT_CONFIG_PENDING") {
        component.append(node("div", "QQ 已登录，正在等待 NapCat 把默认 OneBot 配置落成账号专属配置。", "olivia-chat-copy"));
      } else if (napcat.state === "ONEBOT_READY") {
        component.append(node("div", "QQ 已登录，账号专属 OneBot 配置和 127.0.0.1:3001 都已验证。", "olivia-chat-copy"));
      } else if (napcat.installed) {
        component.append(node("div", "Olivia 会使用 NapCat 的自包含 Node 包，自动准备仅监听 127.0.0.1 的 OneBot 连接和随机 Token。", "olivia-chat-copy"));
      } else {
        component.append(node("div", "点击一键安装即可。Olivia 不会把 NapCat 打进补丁包，而是从 NapCat 发布页下载固定版本并校验 SHA-256。", "olivia-chat-copy"));
      }
      if (napcat.error) renderError(component, {code: napcat.error});
      box.append(component);

      if (napcat.state === "ONEBOT_READY") {
        const managed = node("div", null, "olivia-chat-subcard");
        managed.append(node("div", "OneBot 已就绪", "olivia-chat-name"));
        managed.append(node("div", "机器人 QQ 已由 NapCat 自动识别并复核。现在只需要填写你自己的 QQ 号。", "olivia-chat-copy"));
        const owner = input("你的 QQ 号");
        owner.style.marginTop = "10px";
        managed.append(owner);
        const controls = node("div", null, "olivia-chat-actions");
        controls.append(action("连接并保存", async () => {
          try {
            await request(QQ_CONFIGURE, {method: "POST", body: {managed: true, owner: owner.value.trim()}});
            await refresh(false);
          } catch (error) { renderError(managed, error); }
        }));
        managed.append(controls);
        box.append(managed);
      }

      if (status.e2e_verified_at?.qq) {
        box.append(node("div", "端到端连接已验证：QQ 客户端确实收到了 Olivia 的测试回复。", "olivia-chat-copy"));
      } else if (listener === "CONNECTED") {
        const pending = (status.connection_test_pending || []).includes("qq");
        box.append(node("div",
          pending
            ? "连接测试进行中：请把 QQ 里收到的 4 位验证码原样回复。"
            : "协议连接正常。请在 QQ 里发送 /连接测试；收到 4 位验证码后原样回复，完成真实端到端验证。",
          "olivia-chat-copy"));
      }

      box.append(renderAdvancedQQ(box));
      if (status.qq?.error) renderError(box, {code: status.qq.error});
      return box;
    };

    const schedule = (status) => {
      if (pollTimer !== null) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
      const wechatBusy = ["STARTING", "SCAN_REQUIRED", "SCANNED", "VERIFY_REQUIRED"].includes(status.wechat?.state);
      const napcatBusy = ["DOWNLOADING", "INSTALLER_READY", "INSTALLER_OPENED", "STARTING", "AWAITING_QQ_LOGIN", "ONEBOT_PROBING", "ONEBOT_CONFIG_PENDING"].includes(status.napcat?.state);
      const transportBusy = Object.values(status.listeners || {}).some((value) => ["CONNECTING", "RECONNECTING"].includes(value));
      if (wechatBusy || napcatBusy || transportBusy) pollTimer = setTimeout(() => refresh(true), 2000);
    };

    async function refresh(silent = false) {
      try {
        const status = await request(STATUS);
        const selected = Array.isArray(status.selected_channels) ? status.selected_channels : [];
        const fragment = document.createDocumentFragment();
        if (!selected.length && status.contact_state === "invited") {
          fragment.append(renderChannelChoice());
        } else if (!selected.length) {
          const message = status.contact_state === "eligible"
            ? "关系条件已经满足，等待林离自然提出交换联系方式。"
            : "还没有可绑定的聊天渠道。收到林离的联系方式邀请后，这里会自动打开选择入口。";
          fragment.append(node("div", message, "olivia-chat-copy"));
        } else {
          if (selected.includes("wechat")) fragment.append(renderWechat(status));
          if (selected.includes("qq")) fragment.append(renderQQ(status));
        }
        content.replaceChildren(fragment);
        schedule(status);
      } catch (error) {
        if (!silent) content.replaceChildren(node("div", error?.code || error?.message || "读取聊天设置失败", "olivia-chat-error"));
      }
    }

    refresh(false);
    return true;
  };

  mount();
  const observer = new MutationObserver(() => { mount(); });
  observer.observe(document.documentElement, {childList: true, subtree: true});
})();
'''


__all__ = ["PERSONAL_CHAT_SETUP_JAVASCRIPT"]
