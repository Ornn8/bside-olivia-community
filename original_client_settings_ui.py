"""Self-contained script injected into the original Olivia settings surface."""

from __future__ import annotations


SETTINGS_UI_VERSION = "p03.original-settings-manage.v33"

BOOTSTRAP_JAVASCRIPT = r'''(() => {
  "use strict";

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
  const LLM_TEST_PATH = "/toy/setup/llm/test";
  const LLM_SAVE_PATH = "/toy/setup/llm/save";
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
  const VIDEO_CAPABILITY_BUNDLES = ["ordinary_video", "music_video"];
  const VIDEO_REPLY_DEPENDENCY_LABELS = new Map([
    ["voice_reference", "受管林离音色"],
    ["livetalking", "实时驱动（LiveTalking，可选）"],
    ["latentsync", "口型视频（LatentSync）"],
    ["minimax_music3", "音乐生成（MiniMax Music 3）"],
    ["roformer", "人声分离（RoFormer）"],
    ["official_video_assets", "Olivia 场景与转场素材"],
    ["ffmpeg", "媒体工具（FFmpeg）"],
    ["media_workspace", "媒体工作目录"],
  ]);
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
        ? params.progress === "1"
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
          || path === MEMORY_RETRY_PATH)
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
    const endpoint = new URL(path, apiBase);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 25000);
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
      if (!response.ok || !payload || typeof payload.status !== "string") {
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

  const memoryClearFailureMessage = (error) => {
    if (error && error.code === "MEMORY_ADMIN_BUSY") {
      return "正在导入或写入记忆，请完成后再清空；本次未执行清空。";
    }
    const code = error && typeof error.code === "string" && /^MEMORY_[A-Z0-9_]{1,80}$/.test(error.code)
      ? `（${error.code}）` : "";
    return `长期记忆清空未完成${code}，原始信件和林离世界保持不变。`;
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
        text("p", "暂无长期记忆。", "text-text-secondary text-body-m font-regular")
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
      const created = formatTime(memory.created_at);
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
        controls.append(correct, remove);
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
    statusNode.dataset.state = "available";
    const failed = Object.entries(capabilities).filter(([, value]) =>
      value && (value.state === "unavailable" || value.state === "degraded"));
    if (failed.length) {
      const labels = {memory: "长期记忆", private_world: "林离世界", candidates: "记忆候选"};
      statusNode.textContent = "本机陪伴服务已连接；" + failed.map(([name, value]) => {
        const code = typeof value.reason_code === "string" && /^[A-Z][A-Z0-9_]{0,95}$/.test(value.reason_code)
          ? `（${value.reason_code}）` : "";
        return `${labels[name] || "部分功能"}暂不可用${code}`;
      }).join("；") + "。";
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

    const heading = text("h3", "长期记忆（Mem0 + BGE）", "text-text-title text-title-m");
    const paused = capability && capability.reason_code === "MEMORY_ADMIN_PAUSED";
    const summary = text("p", "", "text-text-secondary text-body-m font-regular");
    const updateSummary = (latest) => {
      const count = latest && latest.count;
      const isPaused = latest && latest.reason_code === "MEMORY_ADMIN_PAUSED";
      summary.textContent = `状态：${isPaused ? "已暂停（不检索、不写入）" : stateLabels[capabilityState(latest)]}${Number.isInteger(count) ? `，共 ${count} 条` : ""}`;
    };
    updateSummary(capability);
    const controls = document.createElement("div");
    controls.style.display = "flex";
    controls.style.gap = "10px";
    controls.style.alignItems = "center";

    const input = document.createElement("input");
    input.type = "search";
    input.maxLength = 500;
    input.placeholder = "搜索长期记忆";
    input.setAttribute("aria-label", "搜索长期记忆");
    input.className = "flex-1 min-w-0 rounded-3 border border-grey-5 bg-transparent px-4 py-2.5 text-text-body text-body-m";

    const resultState = text(
      "p",
      "正在读取长期记忆……",
      "text-text-secondary text-body-m font-regular"
    );
    resultState.setAttribute("aria-live", "polite");
    const list = stack();

    const load = async () => {
      resultState.textContent = "正在读取长期记忆……";
      list.replaceChildren();
      try {
        const payload = await requestJson(MEMORY_PATH, {
          query: input.value.trim(),
          limit: 50,
        });
        renderMemories(list, payload.memories, load, resultState);
        {
          const latestStatus = await requestJson(STATUS_PATH);
          const latestCapabilities = latestStatus.capabilities && typeof latestStatus.capabilities === "object"
            ? latestStatus.capabilities
            : {};
          const latestMemory = latestCapabilities.memory;
          updateSummary(latestMemory);
        }
        resultState.textContent = input.value.trim()
          ? `搜索结果：${Array.isArray(payload.memories) ? payload.memories.length : 0} 条`
          : "已读取本机长期记忆。";
      } catch (_error) {
        updateSummary({state: "unavailable"});
        resultState.textContent = "长期记忆暂时无法读取。";
      }
    };

    const search = button("搜索", load);
    controls.append(input, search);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        load();
      }
    });

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
    panel.replaceChildren(heading, summary, lifecycleControls, controls, resultState, list);
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
          reasonCode ? `原因代码：${reasonCode}` : "原因代码：无",
          "text-text-secondary text-body-m font-regular"
        )
      );
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
        content.replaceChildren(text("p", "读取关系状态…", "text-text-secondary text-body-m"));
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
      return area;
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
      const projects = section("最近在忙", "有些事会慢慢来，也可以暂时搁下。");
      const shared = section("与你有关", "推荐、约定，以及你留下的参与。");
      for (const [target, items, empty, limit] of [[projects, payload.projects, "她还没有提起正在忙的事。", 3], [shared, payload.shared, "你们的共同事项会从信件中慢慢留下来。", 2]]) {
        const more = document.createElement("details");
        more.append(text("summary", "其他事项与已结束的约定", "text-text-secondary text-caption-m"));
        let shown = 0;
        for (const item of items) {
          const el = document.createElement("details");
          el.style.cssText = "padding:8px 0;overflow-wrap:anywhere";
          el.append(text("summary", `${item.title} · ${labels[item.status] || "进展未知"}`, "text-text-title text-label-l"),
            text("p", item.detail, "text-text-body text-body-m"),
            text("small", when(item.updated_at), "text-text-secondary text-caption-m"));
          topic(el, item);
          if (!["completed", "cancelled"].includes(item.status) && shown < limit) { target.append(el); shown++; }
          else more.append(el);
        }
        if (more.children.length > 1) target.append(more);
        if (!items.length) target.append(text("p", empty, "text-text-secondary text-body-m"));
      }
      const moments = section("生活片段", "最近 3 条，展开可读全文。");
      const recent = payload.moments.filter(item => !payload.current || item.id !== payload.current.source_id).slice(0, 3);
      for (const moment of recent) moments.append(momentRow(moment));
      if (!recent.length) moments.append(text("p", "新的生活片段会慢慢留下来。", "text-text-secondary text-body-m"));
      status.textContent = payload.refreshing ? "正在整理新的近况，已有内容仍可阅读。"
        : payload.error_code ? "新近况暂时没能整理好，已有记录已保留。可以稍后重试。" : "近况已保存。";
      if (panel.dataset?.worldMain !== undefined) {
        const top=actions();top.className='olivia-world-heading';
        const title=stack();title.append(heading,text('p','上海 · 北京时间','text-text-secondary'));top.append(title,button('更新近况',()=>load(true)));
        const columns=document.createElement('div');columns.className='olivia-world-columns';
        const primary=document.createElement('div'),secondary=document.createElement('aside');
        const expanded=panel._worldExpanded || (panel._worldExpanded=['now','projects']);
        const fold=(content,key,column)=>{
          const area=content.tagName==='DETAILS'?content:document.createElement('details');
          const caption=area===content?content.querySelector('summary'):document.createElement('summary');
          if(area!==content){caption.textContent=content.querySelector('h4').textContent;content.querySelector('h4').remove();area.append(caption)}
          const body=document.createElement('div');body.className='olivia-world-scroll';body.tabIndex=0;body.setAttribute('role','region');body.setAttribute('aria-label',caption.textContent);
          if(area===content){for(const child of [...area.children])if(child!==caption)body.append(child)}else body.append(content);
          area.append(body);area.classList.add('olivia-world-fold');area.dataset.worldSection=key;area.open=expanded[column]===key;
          area.addEventListener('toggle',()=>{
            if(!area.isConnected)return;
            if(area.open){expanded[column]=key;for(const peer of area.parentElement.children)if(peer!==area)peer.open=false}
            else if(expanded[column]===key)expanded[column]=null;
          });
          return area;
        };
        primary.append(fold(now,'now',0),fold(moments,'moments',0),fold(historyPanel(),'history',0));
        secondary.append(fold(projects,'projects',1),fold(shared,'shared',1),fold(relationshipPanel(),'relationship',1));columns.append(primary,secondary);
        panel.replaceChildren(top,status,columns);
      } else panel.replaceChildren(heading, status, button("更新近况", () => load(true)), now, projects, shared, moments, historyPanel(), relationshipPanel());
    };
    const load = async (refresh = false) => {
      if (busy || !alive()) return;
      busy = true;
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
        if (payload.refreshing) window.setTimeout(() => load(), 1500);
      } catch (_error) {
        if (alive()) {
          status.textContent = "近况暂时无法读取，请稍后重试。";
          if (panel.children.length <= 2) panel.replaceChildren(heading, status, button("重试", () => load()));
        }
      } finally { busy = false; }
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

  const renderLlmSetupPanel = async (panel, initialMode) => {
    panel.replaceChildren(
      text("h3", "大模型连接", "text-text-title text-title-m"),
      text("p", "API key 仅加密保存在这台电脑上，不会显示在页面或日志中。", "text-text-secondary text-body-m font-regular")
    );
    let setup;
    try {
      setup = await requestSetup(SETUP_STATUS_PATH);
    } catch (_error) {
      panel.append(text("p", "初始设置服务暂不可用。", "text-text-secondary text-body-m font-regular"));
      return;
    }
    const provider = document.createElement("select");
    provider.className = "rounded-3 border border-grey-5 bg-transparent px-4 py-2.5 text-text-body text-body-m";
    const qwenBaseUrl = "https://dashscope.aliyuncs.com/compatible-mode/v1";
    const qwenModels = ["qwen3.8-max", "qwen3.8-flash"];
    const isDeepSeekEndpoint = (value) => /^https:\/\/api\.deepseek\.com(?:\/v1)?\/?$/i.test(value.trim());
    const isQwenEndpoint = (value) => (
      /^https:\/\/dashscope\.aliyuncs\.com\/compatible-mode\/v1\/?$/i.test(value.trim())
      || /^https:\/\/[a-z0-9][a-z0-9-]*\.[a-z0-9-]+\.maas\.aliyuncs\.com\/compatible-mode\/v1\/?$/i.test(value.trim())
    );
    for (const [value, label] of [
      ["deepseek", "DeepSeek 官方"],
      ["opencode-go", "OpenCode Go"],
      ["qwen", "阿里云百炼 Qwen"],
      ["custom", "自定义 OpenAI 兼容接口"],
    ]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      provider.append(option);
    }
    const providerLabel = document.createElement("label");
    providerLabel.style.display = "grid";
    providerLabel.style.gap = "6px";
    providerLabel.append(
      text("span", "服务商", "text-text-secondary text-body-m font-regular"),
      provider
    );
    const base = setupInput("接口地址");
    const model = setupInput("模型");
    const key = setupInput("API key（自定义接口留空表示无需鉴权；预设接口留空沿用已保存的 key）", "password");
    base.input.maxLength = 512;
    model.input.maxLength = 128;
    key.input.maxLength = 512;
    base.input.value = setup.llm.base_url || "https://api.deepseek.com";
    model.input.value = setup.llm.model || (
      isQwenEndpoint(base.input.value)
        ? qwenModels[0]
        : "deepseek-v4-pro"
    );
    key.input.value = "";
    const inferProvider = () => {
      if (isDeepSeekEndpoint(base.input.value)) return "deepseek";
      if (base.input.value === "https://opencode.ai/zen/go/v1") return "opencode-go";
      if (isQwenEndpoint(base.input.value)) return "qwen";
      return "custom";
    };
    provider.value = inferProvider();
    provider.addEventListener("change", () => {
      if (provider.value === "deepseek") {
        base.input.value = "https://api.deepseek.com";
        model.input.value = "deepseek-v4-pro";
      } else if (provider.value === "opencode-go") {
        base.input.value = "https://opencode.ai/zen/go/v1";
        model.input.value = "deepseek-v4-pro";
      } else if (provider.value === "qwen") {
        const currentBase = base.input.value.trim();
        if (!isQwenEndpoint(currentBase)) {
          base.input.value = qwenBaseUrl;
        }
        model.input.value = qwenModels[0];
      }
      invalidateTest();
      updateModelControl();
      if (provider.value === "deepseek" || provider.value === "qwen") void syncModels();
    });
    const state = text("p", "请先测试连接。自定义本地接口无需 key 时可留空；需要鉴权时请填写 key。", "text-text-secondary text-body-m font-regular");
    state.setAttribute("aria-live", "polite");
    const currentConfig = () => ({
      base_url: base.input.value.trim(),
      model: model.input.value.trim(),
      api_key: key.input.value.trim(),
    });
    let testedConfig = null;
    let setupBusy = false;
    const matchesTest = () => {
      const current = currentConfig();
      return testedConfig !== null && Object.keys(current).every(name => current[name] === testedConfig[name]);
    };
    const invalidateTest = () => {
      const valid = matchesTest();
      setButtonsBusy([save], setupBusy || !valid);
      if (!setupBusy && testedConfig !== null) {
        state.textContent = valid ? "连接成功，可以保存。" : "配置已变化，请重新测试连接。";
      }
    };
    const testConnection = button("测试连接", async () => {
      const requestedConfig = currentConfig();
      testedConfig = null;
      setupBusy = true;
      setButtonsBusy([testConnection, save], true);
      state.textContent = "正在测试连接……";
      try {
        await requestSetup(LLM_TEST_PATH, requestedConfig);
        testedConfig = requestedConfig;
      } catch (_error) {
        state.textContent = "连接失败，请检查地址、模型和 API key。";
        save.disabled = true;
      } finally {
        setupBusy = false;
        testConnection.disabled = false;
        testConnection.style.opacity = "1";
        testConnection.style.cursor = "pointer";
        invalidateTest();
      }
    });
    const save = button("保存", async () => {
      if (setupBusy || !matchesTest()) {
        if (!setupBusy) state.textContent = "请重新测试连接后保存。";
        return;
      }
      const requestedConfig = currentConfig();
      setupBusy = true;
      setButtonsBusy([testConnection, save], true);
      state.textContent = "正在安全保存……";
      try {
        await requestSetup(LLM_SAVE_PATH, requestedConfig);
        key.input.value = "";
        setup.llm.key_configured = Boolean(requestedConfig.api_key) || setup.llm.key_configured;
        void syncModels();
        state.textContent = "已保存。下一次发送立即生效。";
      } catch (_error) {
        state.textContent = "保存失败，请重新测试连接。";
      } finally {
        setupBusy = false;
        testedConfig = null;
        testConnection.disabled = false;
        testConnection.style.opacity = "1";
        testConnection.style.cursor = "pointer";
        save.disabled = true;
      }
    });
    save.disabled = true;
    const removeKey = button("删除 API key", async () => {
      if (!await confirmAction("确认删除这台电脑上保存的 API key？")) {
        return;
      }
      testedConfig = null;
      setupBusy = true;
      setButtonsBusy([testConnection, save, removeKey], true);
      try {
        await requestSetup(LLM_DELETE_PATH, {});
        key.input.value = "";
        state.textContent = "API key 已删除。下一次发送立即生效。";
      } catch (_error) {
        state.textContent = "API key 删除失败，请重试。";
      } finally {
        setupBusy = false;
        testConnection.disabled = false;
        removeKey.disabled = false;
        testConnection.style.opacity = "1";
        removeKey.style.opacity = "1";
        invalidateTest();
      }
    });
    base.input.addEventListener("input", invalidateTest);
    model.input.addEventListener("input", invalidateTest);
    key.input.addEventListener("input", invalidateTest);
    const controls = actions();
    controls.append(testConnection, save);
    if (setup.llm.key_configured) {
      controls.append(removeKey);
    }
    panel.append(providerLabel, base.wrapper, model.wrapper, key.wrapper, controls, state);
    const modelSelect = document.createElement("select");
    modelSelect.className = provider.className;
    modelSelect.style.width = "100%";
    modelSelect.setAttribute("aria-label", "模型");
    const modelStatus = text("p", "", "text-text-secondary text-body-m font-regular");
    modelStatus.setAttribute("aria-live", "polite");
    let modelRequest = 0;
    let knownModels = [];
    const updateModelControl = () => {
      const official = isDeepSeekEndpoint(base.input.value);
      const qwen = provider.value === "qwen";
      model.input.hidden = official || qwen;
      modelSelect.hidden = !official && !qwen;
      refreshModels.hidden = !official;
      modelStatus.hidden = !official && !qwen;
      const selected = model.input.value;
      modelSelect.replaceChildren();
      const choices = qwen ? [...qwenModels, selected] : [selected, ...knownModels];
      for (const value of [...new Set(choices)].filter(Boolean)) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        option.style.background = "#222426";
        modelSelect.append(option);
      }
      modelSelect.value = selected;
    };
    const syncModels = async () => {
      updateModelControl();
      if (provider.value === "qwen") {
        modelStatus.textContent = "可选 qwen3.8-max 或 qwen3.8-flash；业务空间可填写专属 OpenAI 兼容地址。";
        return;
      }
      if (modelSelect.hidden) return;
      if (!key.input.value.trim() && !setup.llm.key_configured) {
        modelStatus.textContent = "填写 API key 后自动获取模型列表。";
        return;
      }
      const serial = ++modelRequest;
      const requested = currentConfig();
      modelStatus.textContent = "正在获取模型列表…";
      try {
        const result = await requestSetup("/toy/setup/llm/models", requested);
        if (serial !== modelRequest || requested.base_url !== currentConfig().base_url || requested.api_key !== currentConfig().api_key) return;
        if (!Array.isArray(result.models) || !result.models.length) throw new Error();
        knownModels = result.models;
        updateModelControl();
        modelStatus.textContent = "模型列表已同步，当前选择保持不变。";
      } catch (_error) {
        if (serial !== modelRequest || requested.base_url !== currentConfig().base_url || requested.api_key !== currentConfig().api_key) return;
        modelStatus.textContent = "模型列表获取失败，已保留当前模型。请检查服务连接和 Key 后刷新。";
      }
    };
    const refreshModels = button("刷新模型列表", syncModels);
    modelSelect.addEventListener("change", () => {
      model.input.value = modelSelect.value;
      invalidateTest();
    });
    base.input.addEventListener("change", () => { knownModels = []; void syncModels(); });
    key.input.addEventListener("change", () => void syncModels());
    model.wrapper.append(modelSelect, refreshModels, modelStatus);
    updateModelControl();
    void syncModels();
  };

  const formatBytes = (value) => {
    if (!Number.isInteger(value) || value < 0) return "未知";
    if (value < 1024 * 1024) return `${Math.ceil(value / 1024)} KiB`;
    if (value >= 1024 * 1024 * 1024) {
      return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GiB`;
    }
    return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
  };

  const renderMem0CapabilityPanel = async (panel) => {
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
      ready: runtimeLoaded ? "已安装并已加载" : "组件已安装，记忆尚未加载；请查看长期记忆页",
      paused: "已暂停",
      repair: "需修复",
      incompatible: "不兼容",
    };
    const heading = text("h3", "长期记忆", "text-text-title text-title-m");
    const summary = text(
      "p",
      "导入长期记忆离线包，包含 Mem0 与 BGE，无需 GPU。",
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
      await renderMem0CapabilityPanel(panel);
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
    } else if (stateValue === "ready") {
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
          setButtonsBusy([importOffline], false);
        }
      });
      controls.append(importOffline);
    }
    panel.replaceChildren(heading, summary, metadata, controls, result);
  };

  const videoCapabilityViewState = (bundles) => {
    const states = bundles.map((item) => typeof item.state === "string" ? item.state : "missing");
    const state = states.every((value) => value === "ready")
      ? "ready"
      : states.some((value) => ["queued", "downloading", "verifying"].includes(value))
      ? "downloading"
      : states.some((value) => value === "failed")
      ? "failed"
      : states.some((value) => value === "paused")
      ? "paused"
      : states.some((value) => value === "license_review_required")
      ? "license_review_required"
      : states.some((value) => value === "prerequisites_required")
      ? "prerequisites_required"
      : "missing";
    const downloadable = states.some((value) =>
      !["ready", "queued", "downloading", "verifying", "license_review_required", "prerequisites_required"].includes(value)
    );
    const runtimeRequired = states.some((value) => value === "prerequisites_required")
      && states.every((value) => ["ready", "prerequisites_required"].includes(value));
    return { state, downloadable, runtimeRequired };
  };

  const renderMediaComponents = (panel, payload) => {
    const group = payload.components;
    panel.replaceChildren(text("h3", "安装声音与视频组件", "text-text-title text-title-m"),
      text("p", "先按用途找到需要的包，再点击下方按钮选择 ZIP，无需解压。安装组件后，回信形式仍由你的档位设置和信件内容决定。", "text-text-secondary text-body-m font-regular"));
    const list = document.createElement("div");
    list.style.cssText="max-height:360px;overflow:auto;scrollbar-width:thin;scrollbar-color:#66686b transparent";
    const guide = document.createElement("div");
    guide.append(
      text("p", "只收文字信：不用安装这里的组件。", "text-text-body text-body-m font-regular"),
      text("p", "听说话：媒体工具 + 说话语音。听唱歌：媒体工具 + 唱歌；自动识别歌词另加歌词识别。", "text-text-body text-body-m font-regular"),
      text("p", "还要看视频：在相应声音组件上增加口型视频 + 视频场景；唱歌视频还需人声分离。", "text-text-body text-body-m font-regular")
    );
    const legacyProgress = payload.runtime_import || {};
    const progress = ["queued", "extracting", "checking", "testing"].includes(legacyProgress.state) ? legacyProgress : group.progress || {};
    const busy = ["queued", "extracting", "checking", "testing"].includes(progress.state);
    const result = text("p", progress.state === "ready" ? "组件安装完成，请重启程序启用。" : progress.state === "failed" ? "组件安装未完成，原有组件已保留，请重试导入。" : "", "text-text-secondary text-body-m font-regular");
    result.setAttribute("role", "status");
    if (progress.state === "failed" && Array.isArray(progress.failed_components)) {
      const failedNames = group.items.filter(x=>progress.failed_components.includes(x.id)).map(x=>x.label);
      result.textContent = `未完成：${failedNames.join("、")}。其余组件已处理，原有组件保留；可以只重试失败的包。`;
    }
    if (progress.state === "failed") {
      const failures = {
        VIDEO_ARCHIVE_DISK_FULL: "解压写入时磁盘空间不足。请检查 Olivia 数据目录所在盘的可用空间；ZIP 解压后的体积可能远大于压缩包。",
        VIDEO_ARCHIVE_ACCESS_DENIED: "解压时无法读写文件。请检查目录权限和安全软件的拦截记录。",
        VIDEO_ARCHIVE_PATH_TOO_LONG: "解压后的文件路径过长。请使用较短的 Olivia 安装路径。",
        VIDEO_ARCHIVE_IO_FAILED: "读取离线包或写入文件失败。请检查磁盘、外接设备和目录是否可访问。",
        VIDEO_RUNTIME_ARCHIVE_CORRUPT: "ZIP 数据损坏或不完整，请重新下载失败的包。",
        VIDEO_RUNTIME_ARCHIVE_INVALID: "离线包结构不符合要求，请保留诊断包核对版本。",
        MEDIA_COMPONENT_CLEANUP_FAILED: "导入失败，且本次临时文件未能完全清理。请导出诊断包，不要反复重试。"
      };
      result.textContent += " " + (failures[progress.reason_code] || "请导出诊断包查看具体失败原因。");
    }
    if (busy) result.textContent = `正在${({queued:"等待安装",extracting:"解压",checking:"校验",testing:"检查运行环境"})[progress.state]}：${formatBytes(progress.checked_bytes || 0)} / ${formatBytes(progress.total_bytes || 0)}`;
    const batch = button("选择离线包（可多选 ZIP）", async () => {
      batch.disabled = true;
      try {
        const response=await requestCapability(VIDEO_CAPABILITY_ACTION_PATH,{action:"import_components",component_ids:group.items.map(x=>x.id)});
        if(response.status==="CANCELLED") {result.textContent="未选择文件，组件没有变动。点击“选择离线包”可重新选择。";return;}
        if(response.status==="REJECTED") {result.textContent="已有导入任务，请等待完成。";return;}
        await renderVideoCapabilityPanel(panel);
      } catch (_) {result.textContent="导入未能启动，请检查选择的组件 ZIP。";}
      finally {batch.disabled=busy;}
    }); batch.disabled=busy;
    panel.append(guide,list,batch,text("p","在文件窗口中按住 Ctrl 可选择多个 ZIP，点击“打开”开始安装。文件名中的日期可以不同，程序按包内信息识别组件；已安装的无需重复选择。","text-text-secondary text-body-m font-regular"),result);
    group.items.forEach(item=>{
      const row=document.createElement("div");row.className="flex items-center justify-between py-3";row.style.cssText="gap:16px;flex-wrap:wrap;border-bottom:1px solid #343638";
      const copy=document.createElement("div");copy.style.cssText="flex:1;min-width:180px";
      copy.append(text("div",item.label,"text-text-body text-label-l"),text("div",item.description || "","text-text-secondary text-caption-m font-regular"));
      const filename=text("div",`对应文件：Olivia-${item.id}-日期.zip`,"text-text-secondary text-caption-m font-regular");
      filename.style.overflowWrap="anywhere";copy.append(filename);
      row.append(copy,text("span",item.state==="installed"?"已安装，可复用":"未安装","text-text-body text-label-l"));list.append(row);
    });
    panel.append(text("p", "所有组件均通过离线 ZIP 导入，无需解压。歌词识别为可选；长期记忆仍在上方独立管理。", "text-text-secondary text-caption-m font-regular"));
    if (busy) {
      const update = async () => {
        if (!panel.isConnected) return;
        try {
          const next = await requestJson(VIDEO_CAPABILITY_PATH);
          const oldProgress = next.runtime_import || {};
          const current = ["queued", "extracting", "checking", "testing"].includes(oldProgress.state) ? oldProgress : next.components.progress;
          if (!["queued", "extracting", "checking", "testing"].includes(current.state)) { await renderVideoCapabilityPanel(panel); return; }
          result.textContent = `正在${({queued:"等待安装",extracting:"解压",checking:"校验",testing:"检查运行环境"})[current.state]}：${formatBytes(current.checked_bytes || 0)} / ${formatBytes(current.total_bytes || 0)}`;
        } catch (_) { result.textContent = "暂时无法读取进度，正在重新连接。"; }
        panel.videoCapabilityProgressTimer = window.setTimeout(update, 1500);
      };
      panel.videoCapabilityProgressTimer = window.setTimeout(update, 1500);
    }
  };

  const renderVideoCapabilityPanel = async (panel) => {
    const renderGeneration = (Number(panel.videoCapabilityGeneration) || 0) + 1;
    panel.videoCapabilityGeneration = renderGeneration;
    if (panel.videoCapabilityProgressTimer) {
      window.clearTimeout(panel.videoCapabilityProgressTimer);
      panel.videoCapabilityProgressTimer = null;
    }
    if (!panel.childElementCount) {
      panel.replaceChildren(
        text("div", "正在检测本机视频运行环境……", "text-text-body text-label-l"),
        text(
          "div",
          "第一次检测可能需要几分钟，设置页面仍可继续使用。",
          "text-text-secondary text-body-m font-regular"
        )
      );
    }
    let payload = null;
    try {
      payload = await requestJson(VIDEO_CAPABILITY_PATH);
    } catch (_error) {
      payload = null;
    }
    if (panel.videoCapabilityGeneration !== renderGeneration) return;
    if (payload && payload.components && Array.isArray(payload.components.items)) {
      renderMediaComponents(panel, payload);
      if (payload.components.progress?.state === "ready") void refreshVideoReplySetting();
      return;
    }
    panel.replaceChildren(text("p", "组件管理服务暂不可用，请重试或更新程序。", "text-text-secondary text-body-m font-regular"),
      button("重新检测", () => { void renderVideoCapabilityPanel(panel); }));
  };

  const renderCapabilityPanel = async (panel) => {
    const heading = text("h3", "本地组件", "text-text-title text-title-m");
    const summary = text(
      "p",
      "所有组件通过离线包安装。按需要分别导入，已安装组件可以复用。",
      "text-text-secondary text-body-m font-regular"
    );
    const memory = card();
    const video = card();
    panel.replaceChildren(heading, summary, memory, video);
    await Promise.allSettled([
      renderMem0CapabilityPanel(memory),
      renderVideoCapabilityPanel(video),
    ]);
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
          : `未能确认更新结果：${code}。请检查版本后重试。`;
      } finally {
        setButtonsBusy([choose, install, rollback], false);
      }
    });
    const install = button("手动校验并安装", async () => {
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
        result.textContent = `补丁安装失败：${error && error.code ? error.code : "UPDATE_ACTION_UNAVAILABLE"}`;
      } finally {
        setButtonsBusy([choose, install, rollback], false);
      }
    });
    const rollback = button("回滚上一版本", async () => {
      if (!await confirmAction("确认回滚到上一版本？关闭并重新打开 Olivia 后生效。")) return;
      setButtonsBusy([choose, install, rollback], true);
      result.textContent = "正在切换到上一版本……";
      try {
        const payload = await requestUpdate({ action: "rollback" });
        result.textContent = `已回滚到版本 ${payload.version}，关闭并重新打开 Olivia 后生效。`;
      } catch (error) {
        result.textContent = `无法回滚：${error && error.code ? error.code : "UPDATE_ACTION_UNAVAILABLE"}`;
      } finally {
        setButtonsBusy([choose, install, rollback], false);
      }
    });
    const controls = actions();
    controls.append(choose, rollback);
    const manual = document.createElement("details");
    manual.append(text("summary", "手动校验（自动校验不可用时）", "text-text-secondary text-body-m"),
      digest.wrapper, install);
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
      renderLlmSetupPanel(panels.llm, initialMode),
      renderCapabilityPanel(panels.capability),
    ];
    if (initialMode) {
      statusNode.textContent = "先连接大模型；未配置大模型时无法进行真实对话。长期记忆可以稍后按需安装，可在设置 > 本地陪伴中继续。";
      await Promise.allSettled(tasks);
      return;
    }
    tasks.push(Promise.resolve(renderLocalUpdatePanel(panels.update)));
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
          : "导入失败，请检查路径、媒体工具和磁盘空间后重试。";
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
      [data-world-main]{overflow:hidden;background:#191a1c;border-radius:12px;padding:24px;min-height:0;flex:1;display:flex;flex-direction:column;box-sizing:border-box}
      [data-world-main] p{line-height:1.7;margin:8px 0}
      [data-world-main] h3{font-size:26px;margin:0}[data-world-main] h4{font-size:21px;margin:0}
      [data-world-main] button,.olivia-world-header button{border:1px solid #686a70;border-radius:999px;background:transparent;color:#ded9d1;padding:9px 18px;font:inherit;cursor:pointer}
      [data-world-main] summary{cursor:pointer;line-height:1.7}
      [data-world-main] article{background:transparent!important;padding:12px 0!important}
      .olivia-world-heading{display:flex;justify-content:space-between;align-items:center;gap:16px}
      .olivia-world-columns{display:grid;grid-template-columns:minmax(0,1.8fr) minmax(0,1fr);gap:36px;flex:1;min-height:0;overflow:hidden}
      .olivia-world-columns>aside{border-left:1px solid #383a3e;padding-left:32px;min-width:0}
      .olivia-world-columns>div,.olivia-world-columns>aside{min-width:0;min-height:0;display:flex;flex-direction:column;overflow:hidden}
      .olivia-world-fold{margin:0!important;min-height:0;flex:0 0 auto;border-bottom:1px solid #383a3e;overflow:hidden}
      .olivia-world-fold[open]{flex:1 1 0;position:relative}
      .olivia-world-fold>summary{padding:14px 0;font-size:20px;height:64px;box-sizing:border-box}
      .olivia-world-scroll{position:absolute;inset:64px 0 0;min-height:0;overflow-y:auto;overflow-x:hidden;overscroll-behavior:contain;scrollbar-width:none;padding:0 12px 16px 0}
      .olivia-world-scroll::-webkit-scrollbar{display:none;width:0;height:0}
      .olivia-world-scroll>section{margin-top:0!important}
      @media(max-width:800px){.olivia-world-columns{gap:16px;grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.olivia-world-columns>aside{padding-left:16px}[data-world-main]{padding:16px}.olivia-world-fold>summary{font-size:16px;padding:12px 0;height:52px}.olivia-world-scroll{top:52px}}
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

    const heading = text("h2", initialMode ? "欢迎使用 Olivia" : "本地陪伴", "text-text-title text-headline-m");
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
          { id: "llm", label: "大模型", key: "llm" },
          { id: "capability", label: "可选能力", key: "capability" },
        ]
      : [
          { id: "llm", label: "大模型", key: "llm" },
          { id: "capability", label: "本地组件", key: "capability" },
          { id: "update", label: "补丁更新", key: "update" },
          { id: "memory", label: "长期记忆", key: "memory" },
        ];

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
    if (initialMode) {
      const finishActions = actions();
      finishActions.style.marginTop = "18px";
      const finish = button("完成初始设置", async () => {
        setButtonsBusy([finish], true);
        try {
          await finishInitialSetup(false);
          backdrop.remove();
        } catch (_error) {
          status.textContent = "初始设置状态保存失败，请重试。";
          setButtonsBusy([finish], false);
        }
      });
      finishActions.append(finish);
      dialog.append(finishActions);
    }
    backdrop.append(theme, dialog);
    backdrop.addEventListener("click", (event) => {
      if (!initialMode && event.target === backdrop) {
        dismiss();
      }
    });
    backdrop.addEventListener("keydown", (event) => {
      if (!initialMode && event.key === "Escape") {
        dismiss();
      }
    });
    document.body.append(backdrop);
    showPanel(initialPanel);
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
    const timeout = window.setTimeout(() => controller.abort(), 300000);
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
  const confirmReplyRoute = (route, ready, video = false) => new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.setAttribute("aria-label", "确认回信形式");
    dialog.setAttribute("data-olivia-route-confirm", "");
    dialog.style.cssText = "position:fixed;inset:0;margin:auto;background:#191a1c;color:#ded9d1;border:1px solid #66696f;border-radius:16px;padding:28px;max-width:480px;max-height:calc(100% - 48px);overflow:auto;width:calc(100% - 48px);box-sizing:border-box;font-family:inherit;";
    const title = text("h3", ready ? "本次开启回信形式？" : "需要准备回信组件", "text-title-m");
    title.style.marginBottom = "12px";
    const explanation = text("p", ready
      ? `这封信请求了${REPLY_ROUTE_LABELS[route]}${video ? "视频" : ""}，但你已关闭该形式。可以仅为这封信开启，长期设置保持不变。`
      : `这封信请求了${REPLY_ROUTE_LABELS[route]}，当前缺少所需组件。请先在本地组件中准备好，再发送。`, "text-body-m font-regular");
    explanation.style.cssText = "margin-bottom:20px;line-height:1.7;";
    const shade = document.createElement("style");
    shade.textContent = "dialog[data-olivia-route-confirm]::backdrop{background:rgba(0,0,0,.6)}";
    dialog.append(shade, title, explanation);
    const previous = document.activeElement;
    const finish = (accepted) => { dialog.close(); dialog.remove(); previous?.focus(); resolve(accepted); };
    const controls = actions();
    controls.append(button("返回修改", () => finish(false)));
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
        @media(max-width:700px){.olivia-compose-grid{--rail:84px;--gap:8px}.olivia-compose-pane>button{font-size:12px;padding:8px 4px}.olivia-compose-cover{gap:12px}.olivia-compose-cover textarea{padding:12px}}
        @media(prefers-reduced-motion:reduce){.olivia-compose-grid,.olivia-compose-grid .olivia-compose-body{transition:none!important}}
      `;document.head.append(style);
    }
    const state={mode:'letter',draft:input.value,material:null,busy:false,output:'audio'};
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
      state.mode=mode;grid.dataset.mode=mode;
      frame.hidden=mode!=='letter';frame.inert=mode!=='letter';cover.hidden=mode!=='cover';cover.inert=mode!=='cover';
      normal.setAttribute('aria-expanded',String(mode==='letter'));sing.setAttribute('aria-expanded',String(mode==='cover'));
      if(mode==='letter'){audio.pause();setValue(state.draft)}else syncCoverText();
    };
    state.select=select;
    const normal=button('普通信件',()=>select('letter')),sing=button('翻唱歌曲',()=>select('cover'));
    normal.setAttribute('aria-expanded','true');sing.setAttribute('aria-expanded','false');
    frame.before(grid);left.append(normal,frame);right.append(sing,cover);grid.append(left,right);cover.hidden=true;cover.inert=true;
    // Keep full-width content mounted so switching cannot reflow the paper or resize the dialog.
    const sizeBodies=()=>{const css=getComputedStyle(grid);grid.style.setProperty('--body-width',Math.max(0,grid.clientWidth-parseFloat(css.getPropertyValue('--rail'))-parseFloat(css.columnGap))+'px')};
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
        lyrics.value=result.data.lyrics;state.material.cover_language=result.data.language||'unknown';status.textContent='歌词已自动识别，请核对并修正。';
      }catch{status.textContent='未能识别歌词。原曲已保留，可手动填写或重新识别。'}
      finally{clearTimeout(timer);state.busy=false;update()}
    };
    const recognize=button('重新识别',()=>void transcribe());
    const labelRow=document.createElement('div');labelRow.className='olivia-cover-row';labelRow.append(text('span','歌词'),recognize);
    const options=document.createElement('div');options.className='olivia-cover-options';
    for(const [mode,label] of [['audio','音频回信'],['video','视频回信']]){const item=button(label,()=>{state.output=mode;for(const node of options.children)node.setAttribute('aria-pressed',String(node===item))});item.setAttribute('aria-pressed',String(mode==='audio'));options.append(item)}
    const update=()=>{choose.disabled=remove.disabled=lyrics.disabled=state.busy;remove.hidden=!state.material;recognize.disabled=state.busy||!state.material};
    state.materialForSend=()=>{
      if(state.busy)throw Error('原曲还在准备中，请稍候再寄出。');
      if(!state.material)throw Error('请先选择原曲。');
      if(!lyrics.value.trim()){lyrics.focus();throw Error('请填写或识别歌词后再寄出。')}
      return {...state.material,cover_lyrics:lyrics.value,cover_output:state.output};
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
        if(result.data.asr_available)await transcribe();else status.textContent='原曲已上传。请导入歌词识别组件，或手动填写歌词。';
      }catch{status.textContent='上传失败，请检查音频文件后重新选择。之前的草稿和原曲仍保留。'}
      finally{state.busy=false;file.value='';update()}
    };
    cover.append(status,sourceRow,player,labelRow,lyrics,options,text('p','翻唱完成后会出现在信箱中，也可收藏到曲库。'));
    const hint=text('p','切换保留内容 · 寄出当前展开的内容');hint.className='olivia-compose-hint';grid.after(hint);update();
    const shell=owner.closest('[role="dialog"],.el-dialog')||owner.parentElement;
    const clear=Array.from(shell.querySelectorAll('button')).find(node=>node.textContent.trim()==='清空');
    clear?.addEventListener('click',event=>{
      if(state.mode!=='cover')return;
      event.preventDefault();event.stopImmediatePropagation();
      if(state.busy){status.textContent='原曲仍在准备中，请完成后再清空。';return}
      remove.click();status.textContent='翻唱内容已清空，普通信件草稿保留。';
    },true);
    // Release only this editor's media when the native composer is removed.
    const dispose=new MutationObserver(()=>{if(grid.isConnected)return;audio.pause();waveCleanup?.();bodyResize.disconnect();if(objectUrl)URL.revokeObjectURL(objectUrl);dispose.disconnect()});dispose.observe(document.body,{childList:true,subtree:true});
  };
  window.__oliviaPrepareLetterRoute = async (config) => {
    const endpoint = new URL(config.url, config.baseURL || apiBase);
    if (endpoint.origin !== new URL(apiBase).origin || !/^\/(?:toy\/)?letter\/send$/.test(endpoint.pathname)) return config;
    if (proactiveState.busy) {
      const error = new Error("林离正在写信，完成后就可以寄出。草稿会保留。");
      error.code = "PROACTIVE_LETTER_BUSY";
      error.config = config;
      throw error;
    }
    const body = typeof config.data === "string" ? JSON.parse(config.data) : config.data;
    if (!body || typeof body.content !== "string" || !body.content.trim()) return config;
    let preview;
    const editor=coverComposer?.isConnected && coverComposer.value===body.content ? composerCovers.get(coverComposer) : null;
    let attachment=null;
    try{if(editor?.mode==='cover')attachment=editor.materialForSend()}
    catch(error){error.config=config;throw error}
    try { preview = await routeRequest("/toy/letter/route-preview", {content: body.content,
      ...(attachment ? {cover_source_id:attachment.cover_source_id,cover_output:attachment.cover_output} : {})}); }
    catch (error) {
      error.config = config;
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
      const messages = {LLM_QUOTA_EXHAUSTED:"大模型服务余额或额度不足，请检查账户额度后重试。",
        REPLY_ROUTE_CLIENT_TIMEOUT:"回信形式检测超时，请稍后重试。",
        REPLY_ROUTE_CLIENT_CONNECTION:"无法连接回信形式检测服务，请检查本地服务是否运行。",
        REPLY_ROUTE_CLIENT_RESPONSE_INVALID:"本地回信形式检测接口返回格式异常，请导出诊断包。",
        REPLY_ROUTE_CLIENT_UNKNOWN:"前端回信形式检测失败，请导出诊断包。",
        LLM_AUTH_FAILED:"大模型服务认证失败，请检查 API Key 和访问权限。",
        LLM_RATE_LIMITED:"大模型服务请求过于频繁，请稍后重试。",
        LLM_TIMEOUT:"大模型服务响应超时，请稍后重试。",
        REPLY_ROUTE_INVALID_RESULT:"大模型返回的回信形式无法识别，请重试；若持续出现，请导出诊断包。",
        REPLY_ROUTE_INVALID_CONTENT:"信件内容无法用于检测回信形式，请检查后重试。",
        VIDEO_TRIAGE_UNAVAILABLE:"回信形式检测服务暂不可用，请重试；若持续出现，请导出诊断包。",
        REPLY_ROUTE_PREVIEW_EXPIRED:"检测期间回信设置发生变化，请重新寄出。"};
      const code = /^[A-Z][A-Z0-9_]{0,95}$/.test(error.message || "") ? error.message : null;
      const fallback = error.name === "AbortError" ? "回信形式检测超时，请稍后重试。"
        : error instanceof TypeError ? "无法连接回信形式检测服务，请检查本地服务是否运行。"
        : "回信形式检测失败，请导出诊断包。";
      error.message = (messages[code] || fallback) + (code ? `（${code}）` : "") + "信件尚未寄出。";
      throw error;
    }
    let once;
    let videoOnce;
    if (preview.requested_route && (!preview.ready || preview.needs_confirmation || preview.needs_video_confirmation)) {
      if (!await confirmReplyRoute(preview.requested_route, preview.ready, preview.needs_video_confirmation)) {
        const error = new Error("已取消发送，信件内容保留"); error.name = "CanceledError"; error.code = "ERR_CANCELED"; error.__CANCEL__ = true; throw error;
      }
      if (preview.needs_confirmation) once = preview.requested_route;
      if (preview.needs_video_confirmation) videoOnce = preview.requested_route;
    }
    const material = {...(body.material || {}), ...(attachment || {}), route_preview_token: preview.token};
    delete material.filename;
    if (preview.requires_cover_audio && !material.cover_source_id) {
      editor?.select('cover');
      throw Object.assign(new Error("请在翻唱页选择原曲并确认歌词，再点击寄出。"),{config,code:"ERR_CANCELED",__CANCEL__:true});
    }
    delete material.route_allow_once;
    delete material.route_video_once;
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
      input.addEventListener("change", () => {
        dirty = true;
        status.textContent = "设置尚未保存，请点击保存后生效。";
      });
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
    const save = button("保存", async () => {
      setButtonsBusy([save], true);
      status.textContent = "正在保存主动写信设置…";
      try {
        await saveProactiveSettings({
          enabled: enabled.input.checked,
          allow_voice: allowVoice.input.checked,
          login_check_enabled: loginCheck.input.checked,
        });
        dirty = false;
        status.textContent = "主动写信设置已保存。";
      } catch (error) {
        const failures = {PROACTIVE_LOGIN_UNAVAILABLE:"登录启动暂时不可用，可以先关闭登录检查后保存。",
          PROACTIVE_STORAGE_UNAVAILABLE:"设置暂时无法保存，请检查磁盘空间后重试。"};
        status.textContent = failures[error?.code] || "主动写信设置保存失败，请稍后重试。";
      } finally {
        setButtonsBusy([save], false);
      }
    });
    const refresh = button("重新读取", async () => {
      setButtonsBusy([save, refresh], true);
      dirty = false;
      try {
        await refreshProactiveStatus();
        status.textContent = "主动写信设置已更新。";
      } finally {
        setButtonsBusy([save, refresh], false);
      }
    });
    const render = (payload) => {
      if (dirty) return;
      enabled.input.checked = payload.enabled === true;
      allowVoice.input.checked = payload.allow_voice !== false;
      loginCheck.input.checked = payload.login_check_enabled === true;
      if (payload.busy) {
        status.textContent = "林离正在写信。普通寄信暂时锁定。";
      } else if (payload.reason && payload.reason !== "PROACTIVE_STATUS_UNAVAILABLE") {
        const reasons = {disabled:"主动写信已关闭。", waiting:"主动写信已开启，等待合适的时间和话题。", considering:"林离在考虑要不要写信。",
          no_opportunity:"暂时没有新的话题。", deferred:"这次先不写，晚些再看看。", retry_later:"暂时未能完成检查，稍后会重试。"};
        status.textContent = reasons[payload.reason] || "主动写信已开启。";
      }
    };
    proactiveStateListeners.add(render);
    const controls = actions();
    controls.append(save, refresh);
    container.append(optionStyle, heading, description, enabled.row, allowVoice.row, loginCheck.row,
      controls, status);
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
    choices.style.cssText = "display:flex;gap:8px;flex-wrap:wrap";
    const labels = {text:"纯文字",audio:"文字＋声音",video:"文字＋声音＋视频"};
    const descriptions = {text:"通过文字回信。",audio:"可回复文字，也可用说话、唱歌或两者组合的音频。",video:"文字、声音和视频都可使用，由本次内容决定。"};
    let selected = null, busy = false;
    const nodes = {};
    const detail = text("p", "", "text-text-secondary text-body-m font-regular");
    const status = text("p", "正在读取设置…", "text-text-secondary text-caption-m font-regular"); status.setAttribute("role", "status");
    const render = () => {
      Object.entries(nodes).forEach(([key,node])=>{
        node.disabled=busy || selected===null; node.setAttribute("aria-checked",String(selected===key));
        node.style.background=selected===key ? "#ded7cb" : "transparent";
        node.style.color=selected===key ? "#18191b" : "";
      });
      detail.textContent=descriptions[selected] || ""; save.disabled=busy || selected===null;
    };
    Object.entries(labels).forEach(([key,label])=>{
      const choice=button(label,()=>{selected=key;status.textContent="点击保存应用此档位。";render();});
      choice.setAttribute("role","radio"); choice.style.flex="1 1 160px"; nodes[key]=choice; choices.append(choice);
    });
    const save=button("保存",async()=>{
      busy=true;render();
      try { await routeRequest("/toy/settings/reply-routes",{request_id:videoReplyRequestId(),tier:selected}); status.textContent="已保存。已接收的信件继续按原设置处理。"; }
      catch (_) { status.textContent="保存失败，请重试。"; }
      finally {busy=false;render();}
    });
    const hydrate=async()=>{
      try {
        const result=await routeRequest("/toy/settings/reply-routes");
        selected=result.tier || (Object.values(result.routes||{}).some(Boolean) ? "video" : "text");
        if(!labels[selected]) throw Error("invalid tier");
        status.textContent=result.tier_configured===false ? "当前沿用旧设置，保存后统一按所选档位生效。" : "";
      } catch (_) { selected=null;status.textContent="设置读取失败，请重新读取。"; }
      render();
    };
    const controls=actions();controls.append(save,button("离线组件",()=>openDialog(false,"capability")),button("重新读取",()=>{if(!busy)void hydrate();}));
    container.append(choices,detail,controls,status);section.append(container);
    refreshVideoReplySetting=()=>container.isConnected ? hydrate() : Promise.resolve(); void hydrate();
  };

  const mountDiagnosticExport = (section) => {
    const row = document.createElement("div");
    row.className = "flex items-center justify-between px-0 py-3 rounded-3";
    const copy = document.createElement("div");
    copy.className = "flex flex-col gap-0 flex-1 min-w-0";
    const state = text("div", "导出本机脱敏诊断包，文件仅保存到本地。", "text-text-secondary text-caption-m font-regular");
    copy.append(
      text("div", "诊断与反馈", "text-text-body text-label-l"),
      state
    );
    const exportButton = button("导出诊断包", async () => {
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
        state.textContent = `导出失败：${code}`;
      } finally {
        setButtonsBusy([exportButton], false);
      }
    });
    row.append(copy, exportButton);
    section.append(text("div", "诊断与反馈", "text-text-body text-title-m"), row);
  };

  const mountLocalLetterImport = (section) => {
    const importRow = document.createElement("div");
    importRow.className = "flex items-center justify-between px-0 py-3 rounded-3";
    const importCopy = document.createElement("div");
    importCopy.className = "flex flex-col gap-0 flex-1 min-w-0";
    const importState = text("div", "", "text-text-secondary text-caption-m font-regular");
    importState.setAttribute("aria-live", "polite");
    importCopy.append(
      text("div", "导入本地历史信件", "text-text-body text-label-l"),
      text("div", "官方服务器已关闭；这里只读取安装时选择的原版游戏目录中的 letter_pairs.json。本地原信和林离的文字回信会作为只读历史进入信箱，并同步长期记忆与关系状态；不联网读取官方服务器、不导入视频，重复记录自动修复或跳过。", "text-text-secondary text-body-m font-regular"),
      importState
    );
    let importPending = false;
    const missingBackupText = "未在原版游戏目录找到 letter_pairs.json。官方服务器已关闭，请先准备本地备份并放回该目录。";
    const refreshLocalBackup = async () => {
      try {
        const payload = await requestJson(LOCAL_LETTER_IMPORT_PATH);
        importState.textContent = `已找到本地备份，共 ${payload.seen} 封；可新增 ${payload.would_insert} 封，需修复 ${payload.would_update} 封，清理旧乱码重复 ${payload.would_remove} 封，重复 ${payload.duplicates} 封。`;
        return payload;
      } catch (error) {
        importState.textContent = error && error.code === "OFFLINE_LETTER_BACKUP_REQUIRED"
          ? missingBackupText
          : error && error.code === "OFFLINE_LETTER_BACKUP_INVALID"
            ? "本地 letter_pairs.json 格式无效，请更换完整备份后重试。"
            : "暂时无法检查本地备份，请重启 Olivia 后重试。";
        return null;
      }
    };
    const importButton = button("导入本地备份", async () => {
      if (importPending) return;
      const preflight = await refreshLocalBackup();
      if (!preflight) return;
      const changeCount = preflight.would_insert + preflight.would_update + preflight.would_remove;
      if (!await confirmAction(`确认从本地 letter_pairs.json 写入或修复 ${changeCount} 封只读历史信件，并同步长期记忆与关系状态？`)) {
        return;
      }
      setButtonsBusy([importButton], true);
      importButton.textContent = "正在导入并整理记忆";
      importPending = true;
      importState.textContent = "正在读取本地备份并写入信箱……";
      try {
        let payload = await requestJson(LOCAL_LETTER_IMPORT_PATH, {progress: "1"});
        if (payload.status !== "RUNNING") {
          payload = await requestMutation(LOCAL_LETTER_IMPORT_PATH, {background: true});
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
        importState.textContent = `已导入 ${inserted} 封、修复 ${updated} 封、清理旧乱码重复 ${removed} 封，长期记忆新增 ${memoryWritten} 条、复用 ${memoryDuplicates} 条；正在刷新信箱。`;
        importButton.textContent = "已完成";
        window.setTimeout(() => {
          try { window.location.reload(); } catch (_error) { /* native shell may own navigation */ }
        }, 800);
      } catch (error) {
        importState.textContent = error && error.code === "OFFLINE_LETTER_BACKUP_REQUIRED"
          ? missingBackupText
          : error && error.code === "OFFLINE_LETTER_BACKUP_INVALID"
            ? "本地 letter_pairs.json 格式无效，请更换完整备份后重试。"
            : error && error.code === "OFFICIAL_HISTORY_MEMORY_WAIT_TIMEOUT"
              ? "长期记忆准备时间较长，本次尚未开始导入。请等待长期记忆显示可用后，再点击导入。"
            : error && error.code === "OFFICIAL_HISTORY_MEMORY_UNAVAILABLE"
              ? "长期记忆尚不可用，本次尚未开始导入。请到长期记忆页面查看状态；恢复可用后再点击导入。"
            : error && error.code && /^[A-Z][A-Z0-9_]{0,95}$/.test(error.code)
              ? `本地信件导入未完成：${error.code}。请保留诊断包。`
              : "暂时无法读取导入结果，后台任务可能仍在继续。点击可查询进度，请勿重启或重复导入。";
        importButton.textContent = "查看进度 / 导入";
      } finally {
        importPending = false;
        setButtonsBusy([importButton], false);
      }
    });
    importRow.append(importCopy, importButton);
    section.append(
      text("div", "历史信件", "text-text-body text-title-m"),
      importRow
    );
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

    const title = text("div", "本地陪伴", "text-text-body text-title-m");
    const row = document.createElement("div");
    row.className = "flex items-center justify-between px-0 py-3 rounded-3";

    const copy = document.createElement("div");
    copy.className = "flex flex-col gap-0 flex-1 min-w-0";
    copy.append(
      text("div", "记忆与林离世界", "text-text-body text-label-l"),
      text(
        "div",
        "在 Olivia 客户端内查看并管理本地连续性。",
        "text-text-secondary text-body-m font-regular"
      )
    );

    row.append(copy, button("打开", () => openDialog(false)));
    section.append(title, row);
    if (window.__oliviaNativeView) mountProactiveSetting(section);
    mountDiagnosticExport(section);
    mountVideoReplySetting(section);
    mountLocalLetterImport(section);
    container.append(section);
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

  let scheduled = false;
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
    olivia-letter-audio{display:block;margin:var(--tp-spacing-5,16px) var(--tp-spacing-6,16px) 0;color:var(--tp-grey-0,#333);font-family:inherit}
    .mail-box-reply-content-text:has(olivia-letter-audio){height:auto;min-height:290px}
    .mail-box-reply-content-text:has(olivia-letter-audio) .mail-box-reply-content-textarea{height:180px;margin-top:10px}
    olivia-letter-audio .voice-controls{position:relative;display:flex;flex-direction:column;align-items:center;gap:5px;padding:8px 0 12px;color:#514638}
    olivia-letter-audio .voice-controls>button{position:absolute;top:21px;left:calc(50% - 105px)}
    olivia-letter-audio .voice-controls[data-wave-style="ripple"]>button{left:calc(50% - 15px);top:21px;z-index:1}
    .olivia-wave-style{position:fixed;z-index:50;width:108px;border:1px solid #64676e;border-radius:14px;background:#202124;color:#eee9df;font:13px/1.5 "Microsoft YaHei",Arial,sans-serif;padding:6px;cursor:pointer;color-scheme:dark}
    .olivia-wave-style option{background:#202124;color:#eee9df;font:13px "Microsoft YaHei",Arial,sans-serif}
    @media(max-width:600px){olivia-letter-audio .voice-controls{padding-top:32px}olivia-letter-audio .voice-controls>button,olivia-letter-audio .voice-controls[data-wave-style="ripple"]>button{top:45px}}
    olivia-letter-audio button{appearance:none;border:0;background:none;color:inherit;padding:6px;cursor:pointer;flex-shrink:0;line-height:1}
    olivia-letter-audio button:focus-visible,olivia-letter-audio input:focus-visible{outline:2px solid currentColor;outline-offset:3px}
    olivia-letter-audio svg{width:18px;height:18px;fill:currentColor;display:block}
    olivia-letter-audio input{min-width:20px;flex:1;height:3px;accent-color:var(--tp-grey-0,#333);cursor:pointer}
    olivia-letter-audio .voice-wave{position:relative;width:160px;max-width:65%;height:60px;color:#514638}
    olivia-letter-audio .voice-wave canvas{display:block;width:100%;height:60px;pointer-events:none}
    olivia-letter-audio .voice-wave input{position:absolute;left:0;bottom:-5px;width:100%;height:20px;margin:0;opacity:0;touch-action:pan-y}
    olivia-letter-audio .voice-wave:focus-within{outline:1px solid currentColor;outline-offset:3px}
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
        const endpoint=new URL('/toy/cover/progress',coverApi);endpoint.searchParams.set('letter_id',id);
        const response=await fetch(endpoint,{cache:'no-store',credentials:'omit'});if(!response.ok)return;
        const data=(await response.json()).data;const status=this.querySelector('.voice-status');if(!status||!data)return;
        const labels={loading:'正在准备翻唱…',transcribing:'正在识别原曲歌词…',loading_model:'正在加载翻唱模型…',generating:'林离正在翻唱…',decoding:'正在保存歌曲音频…',completed:'歌曲已完成，正在准备回信…'};
        const errors={COVER_LYRICS_REQUIRED:'未能识别歌词，请补充原曲歌词后重新寄信。',COVER_RUNTIME_UNAVAILABLE:'翻唱组件尚未准备完整，请检查本地组件。',COVER_GENERATION_TIMEOUT:'这次翻唱等待超时，可以手动重试。',COVER_SOURCE_REQUIRED:'这封信缺少原曲音频，请重新选择后寄信。'};
        if(['FAILED','UNAVAILABLE'].includes(data.status))status.textContent=errors[data.error_code]||'这次翻唱未能完成，文字回信已保留。';
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
        const row=document.createElement('div');row.className='voice-controls';
        const button=document.createElement('button');button.type='button';icon(button,true);button.setAttribute('aria-label','播放语音');
        const seek=document.createElement('input');seek.type='range';seek.min='0';seek.max='0';seek.step='0.1';seek.value='0';seek.setAttribute('aria-label','语音播放进度');
        const stamp=document.createElement('time');stamp.textContent='0:00';
        const fmt=x=>Math.floor(x/60)+':'+String(Math.floor(x%60)).padStart(2,'0');
        const sync=()=>{icon(button,audio.paused);button.setAttribute('aria-label',audio.paused?'播放语音':'暂停语音');seek.max=String(audio.duration||0);seek.value=String(audio.currentTime);stamp.textContent=fmt(audio.currentTime)+' / '+fmt(audio.duration||0)};
        button.onclick=()=>{this.waveCleanup?.start();if(audio.paused)audio.play().catch(()=>{status.textContent='语音暂时无法播放，请稍后重新打开信件。'});else audio.pause()};
        seek.oninput=()=>{audio.currentTime=Number(seek.value)};
        audio.onplay=()=>{if(current&&current!==audio)current.pause();document.querySelectorAll('video').forEach(v=>v.pause());current=audio;sync()};audio.onpause=sync;audio.ontimeupdate=sync;
        audio.onloadedmetadata=()=>{audio.currentTime=Math.min(oldTime,audio.duration||0);sync();if(wasPlaying)audio.play().catch(()=>{})};
        audio.onerror=()=>{status.textContent='语音暂时无法播放，请稍后重新打开信件。'};
        audio.onended=sync;
        const wave=document.createElement('div');wave.className='voice-wave';
        row.append(button,wave,stamp);this.append(row);this.waveCleanup=letterWave(wave,seek,audio,url);
        const styles=document.createElement('select');styles.className='olivia-wave-style';styles.setAttribute('aria-label','波形样式');styles.title='选择波形样式';
        for(const [value,label] of [['bars','淡墨呼吸'],['dots','浮动墨点'],['ribbon','轻柔声带'],['ripple','声音涟漪']]){const option=document.createElement('option');option.value=value;option.textContent=label;styles.append(option)}
        try{styles.value=localStorage.getItem('olivia.letter.wave-style')||'bars'}catch(_){}if(!styles.value)styles.value='bars';
        const choose=()=>{row.dataset.waveStyle=styles.value;this.waveCleanup.setStyle(styles.value)};
        styles.onchange=()=>{choose();try{localStorage.setItem('olivia.letter.wave-style',styles.value)}catch(_){}};document.body.append(styles);choose();
        const collect=document.createElement('button');collect.type='button';collect.className='olivia-wave-style';collect.textContent='添加到曲库';
        collect.onclick=async()=>{if(collect.disabled)return;collect.disabled=true;
          try {const response=await fetch(new URL('/toy/local-songs/from-letter',coverApi),{method:'POST',credentials:'omit',
            headers:{'Content-Type':'application/json','X-Olivia-Companion-Action':'confirmed'},body:JSON.stringify({letter_id:this.getAttribute('cover-id')})});
            const result=await response.json();if(!response.ok||result.code!==0)throw Error();collect.textContent='已添加到曲库';window.dispatchEvent(new Event('olivia-local-catalog-ready'));
          }catch(_){collect.textContent='添加失败，点击重试';collect.disabled=false;}};
        if(this.getAttribute('cover-id')&&state==='COMPLETED')document.body.append(collect);
        const paper=this.closest('.mail-box-reply-content-text')||this;
        const positionStyle=()=>{
          const rect=paper.getBoundingClientRect();const top=this.getBoundingClientRect().top;
          styles.hidden=rect.bottom<0||top<0||top>window.innerHeight||!this.isConnected;
          styles.style.left=Math.min(window.innerWidth-116,rect.right+10)+'px';
          styles.style.top=Math.max(8,top)+'px';collect.hidden=styles.hidden;collect.style.left=styles.style.left;collect.style.top=Math.max(8,top+42)+'px';
        };
        const styleResize=new ResizeObserver(positionStyle);styleResize.observe(paper);window.addEventListener('resize',positionStyle);document.addEventListener('scroll',positionStyle,true);positionStyle();
        this.styleCleanup=()=>{collect.remove();styles.remove();styleResize.disconnect();window.removeEventListener('resize',positionStyle);document.removeEventListener('scroll',positionStyle,true)};
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

__all__ = ["BOOTSTRAP_JAVASCRIPT", "SETTINGS_UI_VERSION"]
