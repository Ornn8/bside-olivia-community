"""Small settings-page extension for QQ/Weixin personal-chat binding."""

PERSONAL_CHAT_SETUP_JAVASCRIPT = r'''(() => {
  "use strict";

  const script = document.currentScript;
  const apiBase = script?.dataset?.apiBase;
  if (!apiBase || !document.createElement) return;
  const t = (zh, vars) => window.oliviaI18n ? window.oliviaI18n.t(zh, vars) : String(zh).replace(/\{(\w+)\}/g, (m, k) => vars && k in vars ? vars[k] : m);

  const STATUS = "/toy/personal-chat/setup/status";
  const CHANNEL_CHOICE = "/toy/personal-chat/setup/channel-choice";
  const WECHAT_START = "/toy/personal-chat/setup/wechat/start";
  const WECHAT_VERIFY = "/toy/personal-chat/setup/wechat/verify";
  const QQ_CONFIGURE = "/toy/personal-chat/setup/qq/configure";
  const NAPCAT_INSTALL = "/toy/personal-chat/setup/qq/napcat/install";
  const NAPCAT_START = "/toy/personal-chat/setup/qq/napcat/start";
  const NAPCAT_LOGIN = "/toy/personal-chat/setup/qq/napcat/login";
  const NAPCAT_BROWSER = "/toy/personal-chat/setup/qq/napcat/browser";
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
    IDLE: t("尚未设置"),
    STARTING: t("正在启动…"),
    SCAN_REQUIRED: t("等待微信扫码"),
    SCANNED: t("已扫码，请在手机上确认"),
    VERIFY_REQUIRED: t("需要输入微信验证码"),
    TESTING: t("正在检查连接…"),
    READY_RESTART: t("绑定成功，重启 Olivia 后生效"),
    CONNECTING: t("正在连接"),
    CONNECTED: t("已连接"),
    LISTENING: t("已连接"),
    RECONNECTING: t("正在重连"),
    AUTH_REQUIRED: t("登录已失效，需要重新绑定"),
    SETUP_REQUIRED: t("需要设置"),
    CONFIGURED_RESTART: t("已保存，重启 Olivia 后生效"),
    DOWNLOADING: t("正在下载 QQ 组件…"),
    INSTALLER_READY: t("安装器已准备"),
    INSTALLER_OPENED: t("安装窗口已打开"),
    READY: t("QQ 组件已安装"),
    AWAITING_QQ_LOGIN: t("等待 QQ 登录"),
    ONEBOT_PROBING: t("QQ 已登录，正在检查 OneBot"),
    ONEBOT_CONFIG_PENDING: t("QQ 已登录，正在加载 OneBot 配置"),
    ONEBOT_READY: t("QQ 已登录，OneBot 已就绪"),
    RUNNING: t("QQ 组件正在运行"),
    E2E_VERIFIED: t("端到端已验证"),
    PLATFORM_ACCEPTED: t("平台已接受回复"),
    PLATFORM_CONFIRMED: t("平台已确认回复"),
    UNCONFIRMED: t("回复投递未确认"),
    UNSUPPORTED: t("当前系统不支持"),
    FAILED: t("连接失败"),
  }[value] || value || t("未知状态"));

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
      [data-olivia-personal-chat-setup] .olivia-chat-action{border:1px solid #686a70;border-radius:8px;padding:7px 12px;background:#292a2d;color:#f1eee8;font:inherit;cursor:pointer}
      [data-olivia-personal-chat-setup] .olivia-chat-action:hover:not(:disabled){background:#383a3f}
      [data-olivia-personal-chat-setup] .olivia-chat-action:focus-visible{outline:2px solid #ded9d1;outline-offset:3px}
      [data-olivia-personal-chat-setup] .olivia-chat-action:disabled{opacity:.45;cursor:default}
      [data-olivia-personal-chat-setup] .olivia-chat-fields{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
      [data-olivia-personal-chat-setup] .olivia-chat-input{min-width:0;border:1px solid rgba(255,255,255,.14);border-radius:8px;padding:8px 10px;background:#18191c;color:#f1eee8;caret-color:#f1eee8;color-scheme:dark;outline:none}
      [data-olivia-personal-chat-setup] .olivia-chat-input::placeholder{color:#aaa;opacity:1}
      [data-olivia-personal-chat-setup] .olivia-chat-input:focus-visible{outline:2px solid #ded9d1;outline-offset:2px}
      [data-olivia-personal-chat-setup] .olivia-chat-wide{grid-column:1/-1}
      [data-olivia-personal-chat-setup] .olivia-chat-qr{display:block;width:210px;height:210px;object-fit:contain;background:#fff;border-radius:8px;padding:8px;margin:12px auto 0}
      [data-olivia-personal-chat-setup] .olivia-chat-error{font-size:12px;color:#d9a2a2;margin-top:8px;white-space:pre-wrap}
      [data-olivia-personal-chat-setup] .olivia-chat-verify{display:flex;gap:8px;margin-top:10px}
      [data-olivia-personal-chat-setup] details{margin-top:12px}
      [data-olivia-personal-chat-setup] summary{cursor:pointer;color:var(--color-text-secondary,#aaa);font-size:12px}
      [data-olivia-personal-chat-setup] .olivia-chat-step{margin-top:12px;padding:14px 16px;border:1px solid rgba(127,127,127,.22);border-radius:14px}
      [data-olivia-personal-chat-setup] .olivia-chat-step.is-waiting{opacity:.55}
      [data-olivia-personal-chat-setup] .olivia-chat-step-head{display:flex;align-items:center;gap:10px}
      [data-olivia-personal-chat-setup] .olivia-chat-step-num{width:24px;height:24px;border-radius:50%;display:grid;place-items:center;font-size:13px;font-weight:700;background:rgba(127,127,127,.18);color:var(--color-text-body,#eee);flex:none}
      [data-olivia-personal-chat-setup] .olivia-chat-step.is-done .olivia-chat-step-num{background:#3a9d6e;color:#fff}
      [data-olivia-personal-chat-setup] .olivia-chat-pill{margin-left:auto;font-size:12px;padding:1px 10px;border-radius:999px;background:rgba(127,127,127,.16);color:var(--color-text-secondary,#aaa);white-space:nowrap}
      [data-olivia-personal-chat-setup] .olivia-chat-pill.is-ok{background:rgba(58,157,110,.16);color:#3a9d6e}
      [data-olivia-personal-chat-setup] .olivia-chat-step-body{margin-left:34px}
      [data-olivia-personal-chat-setup] .olivia-chat-link{border:0;background:none;padding:0;margin-top:8px;color:#6fa3ef;font:inherit;font-size:13px;cursor:pointer}
      [data-olivia-personal-chat-setup] .olivia-chat-link:hover{text-decoration:underline}
      @media (max-width:650px){[data-olivia-personal-chat-setup] .olivia-chat-fields{grid-template-columns:1fr}}
    `;
    document.head?.append(style);
  };

  const mount = () => {
    if (document.querySelector("[data-olivia-personal-chat-setup]")) return true;
    // Existing WeChat bindings remain compatible; only QQ is offered in settings.
    const anchor = document.querySelector('[data-olivia-group-body="chat"]');
    if (!anchor) return false;
    ensureStyle();

    const root = document.createElement("section");
    root.dataset.oliviaPersonalChatSetup = "true";
    const content = document.createElement("div");
    let qqOwnerDraft = "";
    let qqSaving = false;
    let renderedStatus = null;
    root.append(content);
    anchor.append(root);

    const renderError = (parent, error) => {
      let target = parent.querySelector(".olivia-chat-error");
      if (!target) {
        target = node("div", "", "olivia-chat-error");
        parent.append(target);
      }
      const code = error?.code || error?.message || String(error || t("连接失败"));
      target.textContent = ({
        QQ_SETUP_INVALID: t("请填一个有效的 QQ 号。"),
        PERSONAL_CHAT_GENERATION_INTERRUPTED: t("这次回复生成被中断。连接恢复后，可以重新发送这条消息。"),
        QQ_BOT_AND_OWNER_MUST_DIFFER: t("这里填你自己的 QQ 号，不是林离登录的那个小号。"),
        JEV_BILLING_ACCOUNT_UNAVAILABLE: t("还没有连接 Olivia 账户 Key，或 Key 无法读取。请在设置的“回信服务 → Olivia 账户”获取或导入 Key，点“连接并保存”后再聊天。"),
        OLIVIA_KEY_REQUIRED: t("还没有连接 Olivia 账户 Key，请在设置的“回信服务 → Olivia 账户”获取或导入 Key，点“连接并保存”后再聊天。"),
        JEV_BALANCE_INSUFFICIENT: t("Olivia 账户余额不足，请在“回信服务 → Olivia 账户”充值后再聊天。"),
        JEV_BILLING_HTTP_402: t("Olivia 账户余额不足，请在“回信服务 → Olivia 账户”充值后再聊天。"),
        PERSONAL_CHAT_PROVIDER_QUOTA_EXHAUSTED: t("Olivia 账户余额不足，请在“回信服务 → Olivia 账户”充值后再聊天。"),
        NAPCAT_START_TIMEOUT: t("QQ 组件在一分钟内未就绪。请检查安全软件拦截，并导出诊断包排查；可点击重试。"),
        NAPCAT_LOGIN_OPEN_FAILED: t("登录窗口未能打开。可以直接扫描本页二维码；需要额外验证时，请设置默认浏览器后再点“打开 QQ 登录窗口”。"),
        NAPCAT_LOGIN_UNAVAILABLE: t("暂未取得 QQ 登录信息，请稍后刷新二维码。"),
        NAPCAT_START_FAILED: t("QQ 组件启动后已退出，请重试或导出诊断包排查。"),
        NAPCAT_PORT_IN_USE: t("QQ 登录端口被其他实例占用，请关闭其他 NapCat 实例后重试。"),
        NAPCAT_DEPENDENCY_HASH_MISMATCH: t("QQ 运行依赖校验失败，请重试；Olivia 会重新下载校验，无需重装程序。"),
        NAPCAT_DEPENDENCY_EXTRACT_FAILED: t("QQ 运行依赖解压失败，请检查剩余空间后重试。"),
        NAPCAT_DEPENDENCY_REPAIR_FAILED: t("QQ 运行依赖补齐失败，请关闭 QQ 组件后重试。"),
        NAPCAT_R2_DOWNLOAD_FAILED: t("QQ 组件包下载失败，请检查网络后重试。下载链接会自动重新获取。")
      })[code] || code;
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

    const renderChannelChoice = (selected = []) => {
      const box = node("div", null, "olivia-chat-channel");
      box.append(node("div", t("绑定 QQ"), "olivia-chat-name"));
      box.append(node("div", t("点击下方按钮开始准备 QQ。"), "olivia-chat-copy"));
      const actions = node("div", null, "olivia-chat-actions");
      if (!selected.includes("qq")) actions.append(action(selected.length ? t("添加 QQ") : "QQ", async () => chooseChannel("qq", box)));
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
      left.append(node("div", t("微信"), "olivia-chat-name"), node("div", stateLabel(state), "olivia-chat-state"));
      const start = action(status.configured?.wechat ? t("重新绑定") : t("生成二维码"), async () => {
        try {
          await request(WECHAT_START, {method: "POST", body: {}});
          await refresh(true);
        } catch (error) { renderError(box, error); }
      });
      top.append(left, start);
      box.append(top);
      if (status.e2e_verified_at?.wechat) {
        box.append(node("div", t("端到端连接已验证：微信客户端确实收到了 Olivia 的测试回复。"), "olivia-chat-copy"));
      } else if (listener === "CONNECTED") {
        const pending = (status.connection_test_pending || []).includes("wechat");
        box.append(node("div",
          pending
            ? t("连接测试进行中：请把微信里收到的 4 位验证码原样回复。")
            : t("接收链路正常。请在微信里发送 {command}；收到 4 位验证码后原样回复，完成真实端到端验证。", {command: "/连接测试"}),
          "olivia-chat-copy"));
      }
      if (status.delivery_health?.wechat === "UNCONFIRMED") {
        box.append(node("div", t("最近一次微信回复只得到表面成功响应，客户端投递尚未确认；Olivia 不会自动重发，避免重复消息。"), "olivia-chat-error"));
      }

      if (status.wechat?.qr_data && ["SCAN_REQUIRED", "SCANNED", "VERIFY_REQUIRED"].includes(status.wechat.state)) {
        const image = document.createElement("img");
        image.className = "olivia-chat-qr";
        image.alt = t("微信绑定二维码");
        image.src = status.wechat.qr_data;
        box.append(image);
      }
      if (status.wechat?.state === "VERIFY_REQUIRED") {
        const row = node("div", null, "olivia-chat-verify");
        const code = input(t("手机上显示的验证码"));
        code.inputMode = "numeric";
        code.maxLength = 8;
        const verify = action(t("提交验证码"), async () => {
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
      const summary = node("summary", t("连接已有的 NapCat / OneBot"));
      details.append(summary);
      const fields = node("div", null, "olivia-chat-fields");
      const account = input(t("林离的 QQ 号"));
      const owner = input(t("你的 QQ 号"));
      const url = input(t("OneBot 地址，例如 ws://127.0.0.1:3001"));
      url.value = "ws://127.0.0.1:3001";
      url.classList.add("olivia-chat-wide");
      const token = input(t("OneBot Token（至少 16 位）"), "password");
      token.classList.add("olivia-chat-wide");
      fields.append(account, owner, url, token);
      details.append(fields);
      const controls = node("div", null, "olivia-chat-actions");
      controls.append(action(t("测试并保存"), async () => {
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

    const step = (number, title, pill, {done = false, waiting = false} = {}) => {
      const box = node("div", null, "olivia-chat-step" + (done ? " is-done" : "") + (waiting ? " is-waiting" : ""));
      const head = node("div", null, "olivia-chat-step-head");
      head.append(node("span", done ? "✓" : String(number), "olivia-chat-step-num"), node("span", title, "olivia-chat-name"));
      if (pill) head.append(node("span", pill, "olivia-chat-pill" + (done ? " is-ok" : "")));
      const body = node("div", null, "olivia-chat-step-body");
      box.append(head, body);
      return {box, body};
    };

    const link = (label, handler) => {
      const button = action(label, handler);
      button.className = "olivia-chat-link";
      return button;
    };

    // 1 her QQ: download the component once, then scan to log in.
    const renderLogin = (status) => {
      const napcat = status.napcat || {};
      const ready = napcat.state === "ONEBOT_READY";
      const {box, body} = step(1, t("林离的 QQ"), ready ? t("已登录") : t("未登录"), {done: ready});
      const copy = (value) => body.append(node("div", value, "olivia-chat-copy"));
      if (ready) {
        copy(t("用一个 QQ 小号登录，林离就用这个号和你聊天。"));
      } else if (!napcat.installed && !["DOWNLOADING", "INSTALLER_OPENED"].includes(napcat.state)) {
        copy(t("用一个 QQ 小号当林离的 QQ。第一次要先下载 QQ 组件（约 430 MB，只需一次）。"));
        const actions = node("div", null, "olivia-chat-actions");
        actions.append(action(t("下载并准备"), async () => {
          try {
            await request(NAPCAT_INSTALL, {method: "POST", body: {}});
            await refresh(true);
          } catch (error) { renderError(body, error); }
        }));
        body.append(actions);
      } else if (napcat.state === "DOWNLOADING") {
        copy(t("正在下载 QQ 组件，下载完会自动继续…"));
      } else if (napcat.state === "STARTING") {
        copy(t("正在启动 QQ，第一次会久一点，不用重复点。"));
      } else if (["ONEBOT_PROBING", "ONEBOT_CONFIG_PENDING"].includes(napcat.state)) {
        copy(t("已登录，正在连接…"));
      } else if (napcat.state === "AWAITING_QQ_LOGIN") {
        const login = status.qq_login || {};
        copy(login.logged_in ? t("已登录，正在连接…")
          : login.scanned ? t("已扫码，请在手机 QQ 上点确认。")
          : login.qr_data ? t("用手机 QQ 登录林离要用的那个小号，扫下面的二维码。")
          : t("二维码还没出来，点“刷新二维码”。"));
        if (login.qr_data) {
          const qr = node("img", null, "olivia-chat-qr");
          qr.alt = t("QQ 登录二维码");
          qr.src = login.qr_data;
          body.append(qr);
        }
        if (login.verification_required) copy(t("QQ 要求额外验证，点“打开登录窗口”完成。"));
        const actions = node("div", null, "olivia-chat-actions");
        actions.append(action(t("刷新二维码"), async () => {
          try {
            await request(NAPCAT_LOGIN, {method: "POST", body: {}});
            await refresh(true);
          } catch (error) { renderError(body, error); }
        }));
        actions.append(link(t("扫不了？打开登录窗口"), async (button) => {
          try {
            await request(NAPCAT_BROWSER, {method: "POST", body: {}});
            button.textContent = t("已打开，再点一次可重开");
          } catch (error) { renderError(body, error); }
        }));
        body.append(actions);
      } else {
        copy(t("用一个 QQ 小号当林离的 QQ，点下面的按钮扫码登录。"));
        const actions = node("div", null, "olivia-chat-actions");
        actions.append(action(t("扫码登录"), async (button) => {
          try {
            button.textContent = t("正在启动…");
            await request(NAPCAT_START, {method: "POST", body: {}});
            await refresh(true);
          } catch (error) { button.textContent = t("扫码登录"); renderError(body, error); }
        }));
        body.append(actions);
      }
      if (napcat.error) renderError(body, {code: napcat.error});
      return box;
    };

    // 2 your QQ: the number she talks to; a saved one is shown, not asked again.
    const renderOwner = (status, state) => {
      const ready = status.napcat?.state === "ONEBOT_READY";
      const saved = status.qq?.owner_masked;
      const restart = ["READY_RESTART", "CONFIGURED_RESTART"].includes(state);
      const live = ["CONNECTED", "LISTENING"].includes(state);
      const done = ready && Boolean(saved) && (live || restart);
      const {box, body} = step(2, t("你的 QQ"), done ? t("已保存") : "", {done, waiting: !ready});
      if (!ready) {
        body.append(node("div", t("先完成上一步。"), "olivia-chat-copy"));
        return box;
      }
      const canReuse = status.qq?.can_reuse_owner === true;
      const editor = node("div");
      const owner = input(t("你自己的 QQ 号"));
      owner.value = qqOwnerDraft;
      owner.inputMode = "numeric";
      owner.setAttribute("aria-label", t("你自己的 QQ 号"));
      owner.style.marginTop = "10px";
      owner.addEventListener("input", () => { qqOwnerDraft = owner.value; });
      const save = (body) => async (button) => {
        qqSaving = true;
        owner.disabled = true;
        const label = button.textContent;
        button.textContent = t("正在保存…");
        try {
          await request(QQ_CONFIGURE, {method: "POST", body});
          qqSaving = false;
          await refresh(false);
        } catch (error) { renderError(editor, error); }
        finally {
          qqSaving = false;
          owner.disabled = false;
          button.textContent = label;
        }
      };
      const saveButton = action(t("保存"), async (button) => save({managed: true, owner: owner.value.trim()})(button));
      saveButton.disabled = !owner.value.trim();
      owner.addEventListener("input", () => { saveButton.disabled = qqSaving || !owner.value.trim(); });
      const controls = node("div", null, "olivia-chat-actions");
      controls.append(saveButton);
      editor.append(owner, controls);

      if (saved) {
        const line = node("div", t("你的 QQ：{owner}", {owner: saved}) + (restart ? " · " + t("重启 Olivia 后生效。") : ""), "olivia-chat-copy");
        line.setAttribute("role", "status");
        body.append(line);
        const actions = node("div", null, "olivia-chat-actions");
        if (canReuse && !done) {
          actions.append(action(t("用这个号"), async (button) => save({managed: true, reuse_saved_owner: true})(button)));
        }
        editor.hidden = !qqOwnerDraft;
        if (editor.hidden) actions.append(link(t("换一个号"), async () => { editor.hidden = false; owner.focus(); }));
        body.append(actions, editor);
      } else {
        body.append(node("div", t("填你自己平时用的 QQ 号，林离只和这个号私聊。"), "olivia-chat-copy"), editor);
      }
      return box;
    };

    // 3 try it: send her a message; /连接测试 makes sure both ways work.
    const renderTry = (status, state) => {
      const live = ["CONNECTED", "LISTENING"].includes(state);
      const verified = Boolean(status.e2e_verified_at?.qq);
      const {box, body} = step(3, t("试一下"), verified ? t("能收发") : "", {done: live && verified, waiting: !live});
      if (!live) {
        body.append(node("div", t("前两步完成后，给林离发条消息试试。"), "olivia-chat-copy"));
      } else if (verified) {
        body.append(node("div", t("已经确认过能互相收发消息。"), "olivia-chat-copy"));
      } else {
        const pending = (status.connection_test_pending || []).includes("qq");
        body.append(node("div", pending
          ? t("把 QQ 里收到的 4 位数字原样回给她，就确认好了。")
          : t("用你的 QQ 给林离发一句话，她会回你。想确认一下的话，给她发 {command}，再把收到的 4 位数字回给她。", {command: "/连接测试"}),
          "olivia-chat-copy"));
      }
      const replyHealth = node("div");
      replyHealth.dataset.qqReplyHealth = "true";
      body.append(replyHealth);
      return box;
    };

    const renderQQ = (status) => {
      const box = node("div", null, "olivia-chat-channel");
      const top = node("div", null, "olivia-chat-row");
      const left = document.createElement("div");
      const listener = status.listeners?.qq;
      const setupState = status.qq?.state;
      const state = ["FAILED", "READY_RESTART"].includes(setupState) ? setupState : listener || setupState;
      const liveState = node("div", stateLabel(state), "olivia-chat-state");
      liveState.dataset.qqLiveState = "true";
      left.append(node("div", "QQ", "olivia-chat-name"), liveState);
      top.append(left);
      box.append(top, renderLogin(status), renderOwner(status, state), renderTry(status, state));

      // Everything technical, closed: the component and its connection, and connecting an existing NapCat.
      const napcat = status.napcat || {};
      const advanced = document.createElement("details");
      advanced.append(node("summary", t("高级")));
      advanced.append(node("div", t("QQ 组件 {version}：{state}", {version: napcat.version || "", state: stateLabel(napcat.state)}), "olivia-chat-copy"));
      advanced.append(renderAdvancedQQ(box));
      box.append(advanced);
      if (status.qq?.error) renderError(box, {code: status.qq.error});
      return box;
    };

    const schedule = (status) => {
      if (pollTimer !== null) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
      if (!root.isConnected) return;
      const wechatBusy = ["STARTING", "SCAN_REQUIRED", "SCANNED", "VERIFY_REQUIRED"].includes(status.wechat?.state);
      const napcatBusy = ["DOWNLOADING", "INSTALLER_READY", "INSTALLER_OPENED", "STARTING", "AWAITING_QQ_LOGIN", "ONEBOT_PROBING", "ONEBOT_CONFIG_PENDING"].includes(status.napcat?.state);
      const transportBusy = Object.values(status.listeners || {}).some((value) => ["CONNECTING", "RECONNECTING"].includes(value));
      const connected = Object.values(status.listeners || {}).some((value) => ["CONNECTED", "LISTENING"].includes(value));
      if (wechatBusy || napcatBusy || transportBusy || connected)
        pollTimer = setTimeout(() => refresh(true), wechatBusy || napcatBusy || transportBusy ? 2000 : 10000);
    };

    const reportGroup = (status) => {
      if (typeof CustomEvent !== "function" || typeof root.dispatchEvent !== "function") return;
      const names = {qq: "QQ", wechat: t("微信")};
      const selected = Array.isArray(status.selected_channels) ? status.selected_channels.filter(name => name === 'qq') : [];
      const live = (name) => ["CONNECTED", "LISTENING"].includes(status.listeners?.[name]);
      for (const name of ["qq", "wechat"]) {
        const problem = selected.includes(name) && status.reply_errors?.[name];
        const value = !selected.includes(name) ? "" : problem ? t("{channel} 需要处理", {channel: names[name]})
          : live(name) ? t("{channel} 正常", {channel: names[name]}) : t("{channel} 未连接", {channel: names[name]});
        root.dispatchEvent(new CustomEvent("olivia-group-status", {bubbles: true, detail: {part: name, value: problem ? "" : value}}));
        root.dispatchEvent(new CustomEvent("olivia-group-status", {bubbles: true, detail: {part: name + ":problem", value: problem ? value : ""}}));
      }
      root.dispatchEvent(new CustomEvent("olivia-group-status", {bubbles: true, detail: {part: "none", value: selected.length ? "" : t("还没有绑定")}}));
    };

    const updateReplyHealth = (status) => {
      reportGroup(status);
      const state = content.querySelector("[data-qq-live-state]");
      if (state) state.textContent = stateLabel(["FAILED", "READY_RESTART"].includes(status.qq?.state)
        ? status.qq.state : status.listeners?.qq || status.qq?.state);
      const health = content.querySelector("[data-qq-reply-health]");
      if (health) {
        health.replaceChildren();
        if (status.reply_errors?.qq) {
          health.append(node("div", t("上一条消息没回成："), "olivia-chat-copy"));
          renderError(health, {code: status.reply_errors.qq});
          const failedAt = new Date(status.reply_error_at?.qq || "");
          if (!Number.isNaN(failedAt.getTime())) {
            const time = failedAt.toLocaleString(t("zh-CN"), {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"});
            health.append(node("div", t("（{time} 那条）再发一条消息，就能看出好了没有。", {time}), "olivia-chat-copy"));
          }
        }
      }
    };

    async function refresh(silent = false) {
      if (!root.isConnected) return;
      if (qqSaving) { pollTimer = setTimeout(() => refresh(true), 2000); return; }
      try {
        const status = await request(STATUS);
        if (!root.isConnected) return;
        if (qqSaving) { pollTimer = setTimeout(() => refresh(true), 2000); return; }
        const selected = Array.isArray(status.selected_channels) ? status.selected_channels.filter(name => name === 'qq') : [];
        if (selected.includes("qq") && status.napcat?.state === "AWAITING_QQ_LOGIN") {
          try { status.qq_login = await request(NAPCAT_LOGIN); }
          catch (_) { status.qq_login = {}; }
        }
        // Health changes must remain visible without rebuilding fields while
        // someone types, selects text, or keeps advanced connection fields open.
        const {reply_errors, last_seen_at, delivery_health, ...presentation} = status;
        const signature = JSON.stringify(presentation);
        const editing = content.contains(document.activeElement);
        if (silent && (editing || signature === renderedStatus)) {
          updateReplyHealth(status);
          schedule(status);
          return;
        }
        const fragment = document.createDocumentFragment();
        if (!selected.length) {
          fragment.append(renderChannelChoice());
        } else {
          if (selected.includes("qq")) fragment.append(renderQQ(status));
        }
        content.replaceChildren(fragment);
        renderedStatus = signature;
        updateReplyHealth(status);
        schedule(status);
      } catch (error) {
        if (!root.isConnected) return;
        if (!silent) content.replaceChildren(node("div", error?.code || error?.message || t("读取聊天设置失败"), "olivia-chat-error"));
        pollTimer = setTimeout(() => refresh(true), 10000);
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
