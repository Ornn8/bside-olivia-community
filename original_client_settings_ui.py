"""Self-contained script injected into the original Olivia settings surface."""

from __future__ import annotations


SETTINGS_UI_VERSION = "p03.original-settings-manage.v17-mailbox1"

BOOTSTRAP_JAVASCRIPT = r'''(() => {
  "use strict";

  const loader = document.currentScript;
  const rawApiBase = loader && loader.dataset ? loader.dataset.apiBase : "";
  const ROOT_ATTR = "data-olivia-companion-settings-root";
  const DIALOG_ATTR = "data-olivia-companion-settings-dialog";
  const STATUS_PATH = "/toy/companion/status";
  const MEMORY_PATH = "/toy/companion/memory";
  const PRIVATE_WORLD_PATH = "/toy/companion/private-world";
  const VIDEO_REPLY_SETTINGS_PATH = "/toy/settings/video-reply";
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
      : 5000;
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
          || path === LOCAL_LETTER_IMPORT_PATH)
        && responseBody && responseBody.data
        ? responseBody.data
        : responseBody;
      const valid = path === LOCAL_LETTER_IMPORT_PATH
        ? payload && payload.status === "READY"
          && Number.isInteger(payload.seen)
          && Number.isInteger(payload.would_insert)
          && Number.isInteger(payload.would_update)
          && Number.isInteger(payload.would_remove)
          && Number.isInteger(payload.duplicates)
        : path === VIDEO_REPLY_SETTINGS_PATH
        ? payload && (payload.state === "available" && typeof payload.enabled === "boolean"
          || payload.state === "unavailable" && typeof payload.reason_code === "string")
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
        throw new Error("capability-unavailable");
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

  const scheduleMemoryStatusRefresh = (panel, delay = 1000) => {
    window.clearTimeout(panel.__oliviaMemoryStatusTimer);
    panel.__oliviaMemoryStatusTimer = window.setTimeout(async () => {
      if (!panel.isConnected) return;
      try {
        const status = await requestJson(STATUS_PATH);
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
      && await confirmAction("清空后无法恢复。原始信件和私人世界不会受影响，仍要继续吗？");
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
            resultState.textContent = "长期记忆清空失败，原始信件和私人世界保持不变。";
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
        const install = button("安装 Embedding", async () => {
          if (!await confirmAction("确认下载本地 Embedding 模型？下载仅在此次确认后开始。")) {
            return;
          }
          setButtonsBusy([install], true);
          resultState.textContent = "正在安装 Embedding……";
          try {
            const payload = await requestCapability(MEM0_CAPABILITY_ACTION_PATH, {
              action: "install",
              source: "auto",
            });
            if (["queued", "downloading", "verifying", "ready"].includes(payload.state)) {
              resultState.textContent = "已转入本地能力下载；可在“本地能力与下载”查看进度。";
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
          setButtonsBusy([retry], true);
          try {
            const payload = await requestMutation(MEMORY_RETRY_PATH, {});
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
    const updateSummary = (count) => {
      summary.textContent = `状态：${paused ? "已暂停（不检索、不写入）" : stateLabels[state]}${Number.isInteger(count) ? `，共 ${count} 条` : ""}`;
    };
    updateSummary(capability.count);
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
        if (!input.value.trim()) {
          const latestStatus = await requestJson(STATUS_PATH);
          const latestCapabilities = latestStatus.capabilities && typeof latestStatus.capabilities === "object"
            ? latestStatus.capabilities
            : {};
          const latestMemory = latestCapabilities.memory;
          updateSummary(latestMemory && latestMemory.count);
        }
        resultState.textContent = input.value.trim()
          ? `搜索结果：${Array.isArray(payload.memories) ? payload.memories.length : 0} 条`
          : "已读取本机长期记忆。";
      } catch (_error) {
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
      if (!await confirmAction(`确认${action} Mem0 长期记忆？Archive 和私人世界不会受影响。`)) {
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
        resultState.textContent = "长期记忆清空失败，原始信件和私人世界保持不变。";
      } finally {
        setButtonsBusy([toggle, clear], false);
      }
    });
    lifecycleControls.append(toggle, clear);
    panel.replaceChildren(heading, summary, lifecycleControls, controls, resultState, list);
    await load();
  };

  const renderPrivateWorldPanel = async (panel, privateCapability) => {
    const rawPrivateWorldState = privateCapability
      && typeof privateCapability.state === "string"
      ? privateCapability.state
      : null;
    const privateState = privateWorldState(privateCapability);
    const heading = text("h3", "私人世界状态", "text-text-title text-title-m");
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
    try {
      const payload = await requestJson(PRIVATE_WORLD_PATH);
      const stages = { unknown: "尚未形成", acquaintance: "初识", familiar: "熟悉", close: "亲近" };
      const levelLabels = { unknown: "未知", low: "低", medium: "中", high: "高" };
      const fields = [
        ["familiarity", "熟悉度"], ["trust", "信任"], ["comfort", "舒适"],
        ["closeness", "亲密"], ["tension", "紧张"],
      ];
      const levels = payload && payload.levels;
      if (
        !payload || payload.status !== "READY" || !stages[payload.relationship_stage]
        || !levels || fields.some(([key]) => !levelLabels[levels[key]])
      ) throw new Error("PRIVATE_WORLD_SUMMARY_INVALID");
      panel.replaceChildren(
        heading,
        summary,
        text("p", `关系阶段：${stages[payload.relationship_stage]}`, "text-text-body text-body-m font-regular"),
        text(
          "p",
          fields.map(([key, label]) => `${label}：${levelLabels[levels[key]]}`).join(" · "),
          "text-text-secondary text-body-m font-regular"
        ),
        text("p", "该状态由林离与用户的历史来信和回信形成，不是可手动修改的分数。", "text-text-secondary text-caption-m font-regular")
      );
    } catch (_error) {
      panel.replaceChildren(
        heading,
        summary,
        text("p", "关系状态暂时无法读取。", "text-text-secondary text-body-m font-regular")
      );
    }
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
    for (const [value, label] of [
      ["deepseek", "DeepSeek 官方"],
      ["opencode-go", "OpenCode Go"],
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
    const key = setupInput(setup.llm.key_configured ? "API key（留空则沿用已保存的 key）" : "API key", "password");
    base.input.maxLength = 512;
    model.input.maxLength = 128;
    key.input.maxLength = 512;
    base.input.value = setup.llm.base_url || "https://api.deepseek.com";
    model.input.value = setup.llm.model || "deepseek-v4-flash";
    key.input.value = "";
    const inferProvider = () => {
      if (base.input.value === "https://api.deepseek.com") return "deepseek";
      if (base.input.value === "https://opencode.ai/zen/go/v1") return "opencode-go";
      return "custom";
    };
    provider.value = inferProvider();
    provider.addEventListener("change", () => {
      if (provider.value === "deepseek") {
        base.input.value = "https://api.deepseek.com";
        model.input.value = "deepseek-v4-flash";
      } else if (provider.value === "opencode-go") {
        base.input.value = "https://opencode.ai/zen/go/v1";
        model.input.value = "deepseek-v4-flash";
      }
      save.disabled = true;
    });
    const state = text("p", setup.llm.key_configured ? "已保存 API key。修改前请先测试连接。" : "请输入 API key 并测试连接。", "text-text-secondary text-body-m font-regular");
    state.setAttribute("aria-live", "polite");
    const testConnection = button("测试连接", async () => {
      setButtonsBusy([testConnection, save], true);
      state.textContent = "正在测试连接……";
      try {
        await requestSetup(LLM_TEST_PATH, {
          base_url: base.input.value.trim(),
          model: model.input.value.trim(),
          api_key: key.input.value.trim(),
        });
        state.textContent = "连接成功，可以保存。";
        setButtonsBusy([save], false);
      } catch (_error) {
        state.textContent = "连接失败，请检查地址、模型和 API key。";
        save.disabled = true;
      } finally {
        testConnection.disabled = false;
        testConnection.style.opacity = "1";
        testConnection.style.cursor = "pointer";
      }
    });
    const save = button("保存", async () => {
      setButtonsBusy([testConnection, save], true);
      state.textContent = "正在安全保存……";
      try {
        await requestSetup(LLM_SAVE_PATH, {
          base_url: base.input.value.trim(),
          model: model.input.value.trim(),
          api_key: key.input.value.trim(),
        });
        key.input.value = "";
        state.textContent = "已保存。下一次发送立即生效。";
      } catch (_error) {
        state.textContent = "保存失败，请重新测试连接。";
      } finally {
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
      setButtonsBusy([testConnection, save, removeKey], true);
      try {
        await requestSetup(LLM_DELETE_PATH, {});
        key.input.value = "";
        state.textContent = "API key 已删除。下一次发送立即生效。";
      } catch (_error) {
        state.textContent = "API key 删除失败，请重试。";
      } finally {
        testConnection.disabled = false;
        removeKey.disabled = false;
        testConnection.style.opacity = "1";
        removeKey.style.opacity = "1";
      }
    });
    const invalidateTest = () => { save.disabled = true; };
    base.input.addEventListener("input", invalidateTest);
    model.input.addEventListener("input", invalidateTest);
    key.input.addEventListener("input", invalidateTest);
    const controls = actions();
    controls.append(testConnection, save);
    if (setup.llm.key_configured) {
      controls.append(removeKey);
    }
    panel.append(providerLabel, base.wrapper, model.wrapper, key.wrapper, controls, state);
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
      ready: runtimeLoaded ? "已安装并已加载" : "已安装，重启 Olivia 后加载",
      paused: "已暂停",
      repair: "需修复",
      incompatible: "不兼容",
    };
    const heading = text("h3", "长期记忆", "text-text-title text-title-m");
    const summary = text(
      "p",
      "可选安装 Mem0 与 BGE 中文 Embedding；约 317 MiB 下载，无需 GPU。",
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
    const onlineInstallAvailable = !offlineImport && (
      ["missing", "repair"].includes(stateValue) || stateValue === "paused"
    );
    if (onlineInstallAvailable) {
      const source = document.createElement("select");
      source.className = "rounded-3 border border-grey-5 bg-transparent px-4 py-2.5 text-text-body text-body-m";
      for (const [value, label, disabled] of [
        ["auto", "自动选择（国内源优先）", false],
        ["official", "仅官方源", false],
      ]) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        option.disabled = disabled;
        source.append(option);
      }
      if (
        stateValue === "paused"
        && ["auto", "official"].includes(payload.source)
      ) {
        source.value = payload.source;
      }
      const install = button(stateValue === "paused" ? "继续下载" : "下载并启用", async () => {
        if (!await confirmAction("确认下载长期记忆能力包？下载将在后台继续。")) return;
        setButtonsBusy([install], true);
        result.textContent = "正在启动后台下载……";
        try {
          await requestCapability(MEM0_CAPABILITY_ACTION_PATH, {
            action: stateValue === "paused" ? "resume" : "install",
            source: source.value,
          });
          await refresh();
        } catch (_error) {
          result.textContent = "下载未能启动，请稍后重试。";
          setButtonsBusy([install], false);
        }
      });
      controls.append(source, install);
    } else if (["queued", "downloading", "verifying"].includes(stateValue)) {
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
        if (!await confirmAction("模型删除后重新启用需要再次下载，仍要继续吗？")) return;
        await requestCapability(MEM0_CAPABILITY_ACTION_PATH, {
          action: "uninstall",
          remove_model: true,
        });
        await refresh();
      });
      controls.append(uninstall, removeAll);
    }
    if (["missing", "repair", "paused"].includes(stateValue)) {
      const importOffline = button("断网恢复：导入离线包（ZIP）", async () => {
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
    const known = new Map(
      payload && Array.isArray(payload.bundles)
        ? payload.bundles
          .filter((item) => item && typeof item.id === "string")
          .map((item) => [item.id, item])
        : []
    );
    const bundles = VIDEO_CAPABILITY_BUNDLES.map((id) => known.get(id) || {
      id,
      state: "missing",
      downloaded_bytes: 0,
      total_bytes: 0,
    });
    const { state, downloadable } = videoCapabilityViewState(bundles);
    const canUninstall = payload && payload.can_uninstall === true;
    const verifyingOnly = bundles.some((item) => item.state === "verifying")
      && !bundles.some((item) => ["queued", "downloading"].includes(item.state));
    const downloadedBytes = bundles.reduce((total, item) => total + (Number(item.downloaded_bytes) || 0), 0);
    const totalBytes = bundles.reduce((total, item) => total + (Number(item.total_bytes) || 0), 0);
    const runtimeProgress = payload && payload.runtime_import && typeof payload.runtime_import === "object"
      ? payload.runtime_import
      : { state: "idle", checked_bytes: 0, total_bytes: 0 };
    const runtimePreparing = ["queued", "extracting", "checking", "testing"].includes(runtimeProgress.state);
    const runtimeReasonLabels = {
      VIDEO_RUNTIME_ARCHIVE_REQUIRED: "当前安装中缺少视频运行环境包",
      VIDEO_RUNTIME_ARCHIVE_INVALID: "所选 ZIP 不是完整的视频运行环境包",
      VIDEO_RUNTIME_ROOT_INVALID: "运行环境清单或文件校验失败",
      VIDEO_RUNTIME_NOT_PORTABLE: "运行环境不能在这台电脑上独立启动",
      VIDEO_RUNTIME_TTS_CONFIG_UNAVAILABLE: "林离语音运行环境未准备完整",
      VIDEO_RUNTIME_PROBE_FAILED: "运行环境自检未通过",
      VIDEO_RUNTIME_ENVIRONMENT_WRITE_FAILED: "运行环境配置保存失败",
      VIDEO_RUNTIME_ENVIRONMENT_ACTIVATION_FAILED: "运行环境已安装，但本次启用失败",
      VIDEO_RUNTIME_IMPORT_FAILED: "视频运行环境安装失败",
    };
    const hardwareReasonLabels = {
      BREEZE_TTS_NVIDIA_GPU_REQUIRED: "Breeze TTS 2 需要 NVIDIA 显卡；CPU 和其他显卡尚未验证",
      BREEZE_TTS_10GB_VRAM_REQUIRED: "Breeze TTS 2 实测要求至少 10GB NVIDIA 显存；8GB 尚未验证",
      BREEZE_TTS_GPU_CAPABILITY_UNVERIFIED: "无法确认 NVIDIA 显卡与显存，暂不允许下载或启用",
    };
    const hardware = payload && payload.hardware && typeof payload.hardware === "object"
      ? payload.hardware
      : null;
    const hardwareMessage = hardware && hardware.status !== "READY"
      ? hardwareReasonLabels[hardware.reason_code] || "Breeze TTS 2 的显卡条件尚未满足"
      : hardware && Number(hardware.detected_vram_mib) > 0
      ? `Breeze TTS 2 显卡检查通过：NVIDIA ${Math.round(Number(hardware.detected_vram_mib) / 1024)}GB`
      : "";
    const runtimeStepMessage = (progress) => {
      const checked = Math.max(0, Number(progress.checked_bytes) || 0);
      const total = Math.max(0, Number(progress.total_bytes) || 0);
      const amount = total > 0 ? ` ${formatBytes(checked)} / ${formatBytes(total)}` : "";
      if (progress.state === "queued") return "视频运行环境等待安装。";
      if (progress.state === "extracting") return `正在解压视频运行环境${amount}。`;
      if (progress.state === "checking") return `正在检查视频运行环境${amount}。`;
      if (progress.state === "testing") return "正在测试视频运行环境。";
      if (progress.state === "ready") return "视频运行环境已安装并启用。";
      if (["required", "failed"].includes(progress.state)) {
        return runtimeReasonLabels[progress.reason_code] || "视频运行环境尚未安装完成。";
      }
      return "";
    };
    let videoSourceMode = "auto";
    const heading = text("h3", "视频回信一键安装", "text-text-title text-title-m");
    const summary = text(
      "p",
      "视频回信固定包含说话与音乐。点击一次自动准备语音、音乐、口型、媒体工具和固定场景所需组件；可用组件优先使用国内源，没有国内镜像时使用官方源。",
      "text-text-secondary text-body-m font-regular"
    );
    const item = card();
    const stateLabel = state === "ready"
      ? "已就绪"
      : state === "paused"
      ? "已暂停"
      : state === "failed"
      ? "安装失败，可重试"
      : state === "downloading"
      ? verifyingOnly ? "正在校验安装文件" : "正在下载并安装"
      : ["license_review_required", "prerequisites_required"].includes(state)
      ? runtimePreparing
        ? "组件已下载，正在准备运行环境"
        : "组件已下载，还需准备视频运行环境"
      : "未安装";
    const result = text("div", "", "text-text-secondary text-caption-m font-regular");
    const sourceControls = actions();
    const domesticSource = button("自动选择（可用国内源优先）", () => {
      videoSourceMode = "auto";
      result.textContent = "可用组件优先使用国内源；没有国内镜像的组件会直接使用官方源。";
    });
    const officialSource = button("仅官方源", () => {
      videoSourceMode = "official";
      result.textContent = "下载时仅使用官方源。";
    });
    sourceControls.append(domesticSource, officialSource);
    const progressText = text(
      "div",
      totalBytes
        ? `已处理 ${formatBytes(downloadedBytes)} / ${formatBytes(totalBytes)}`
        : "大小将在安装时按固定清单校验",
      "text-text-secondary text-caption-m font-regular"
    );
    item.append(
      text("div", "视频回信（说话 + 音乐）", "text-text-body text-label-l"),
      text("div", stateLabel, "text-text-secondary text-body-m font-regular"),
      text("div", "语音、音乐、口型和媒体工具会自动准备，无需逐项选择。", "text-text-secondary text-caption-m font-regular"),
      progressText
    );
    if (hardwareMessage) {
      item.append(text("div", hardwareMessage, "text-text-secondary text-caption-m font-regular"));
    }
    item.append(sourceControls);
    const runtimeStatusText = text(
      "div",
      runtimeStepMessage(runtimeProgress),
      "text-text-secondary text-caption-m font-regular"
    );
    const importOffline = button("断网恢复：导入离线包（ZIP）", async () => {
      setButtonsBusy([importOffline], true);
      result.textContent = "请选择 Olivia 完整离线包（ZIP），无需解压。";
      let importFinished = false;
      const updateImportProgress = async () => {
        try {
          const statusPayload = await requestJson(VIDEO_CAPABILITY_PATH);
          if (importFinished) return;
          const progress = statusPayload && statusPayload.runtime_import;
          if (progress && typeof progress === "object") {
            result.textContent = runtimeStepMessage(progress) || "正在导入离线包。";
          }
        } catch (_error) {
          // The active import request remains authoritative; retry on the next tick.
        }
      };
      const progressTimer = window.setInterval(updateImportProgress, 1000);
      try {
        const response = await requestCapability(
          VIDEO_CAPABILITY_ACTION_PATH,
          { action: "import_offline" },
          30 * 60 * 1000
        );
        if (response.status === "CANCELLED") {
          result.textContent = "已取消导入。";
          return;
        }
        importFinished = true;
        await renderVideoCapabilityPanel(panel);
      } catch (_error) {
        importFinished = true;
        try {
          const failed = await requestJson(VIDEO_CAPABILITY_PATH);
          const progress = failed && failed.runtime_import;
          result.textContent = progress && typeof progress === "object"
            ? runtimeStepMessage(progress) || "离线包导入失败，请重新选择完整 ZIP。"
            : "离线包导入失败，请重新选择完整 ZIP。";
        } catch (_statusError) {
          result.textContent = "离线包导入失败，请重新选择完整 ZIP。";
        }
      } finally {
        importFinished = true;
        window.clearInterval(progressTimer);
        setButtonsBusy([importOffline], false);
      }
    });
    importOffline.disabled = state === "downloading" || state === "ready" || runtimePreparing;
    item.append(
      text(
        "div",
        state === "downloading"
          ? "请先暂停当前下载，再导入离线包。"
          : "正常下载会自动安装并启用；仅断网恢复时选择 Olivia 离线包，无需解压。",
        "text-text-secondary text-caption-m font-regular"
      ),
      runtimeStatusText,
      importOffline
    );
    if (state === "downloading") {
      const pause = button("暂停下载", async () => {
        try {
          await requestCapability(VIDEO_CAPABILITY_ACTION_PATH, { action: "pause" });
          await renderVideoCapabilityPanel(panel);
        } catch (_error) {
          result.textContent = "暂停失败，请稍后重试。";
        }
      });
      item.append(pause);
    } else if (downloadable) {
      const actionLabel = state === "paused" ? "继续下载" : state === "failed" ? "失败重试" : "一键下载并安装";
      const install = button(actionLabel, async () => {
        if (!await confirmAction("确认下载并安装视频回信？将自动准备说话与音乐所需的全部组件；下载即表示你已阅读并同意各上游许可证与使用条款。")) return;
        setButtonsBusy([install], true);
        result.textContent = "正在启动后台下载…";
        try {
          for (const dependency of bundles) {
            if (["ready", "queued", "downloading", "verifying", "license_review_required", "prerequisites_required"].includes(dependency.state)) continue;
            const action = dependency.state === "paused" ? "resume" : dependency.state === "failed" ? "retry" : "install";
            await requestCapability(VIDEO_CAPABILITY_ACTION_PATH, {
              action,
              bundle_id: dependency.id,
              source: videoSourceMode,
              accept_licenses: dependency.id === "music_video",
            });
          }
          await renderVideoCapabilityPanel(panel);
        } catch (_error) {
          result.textContent = "下载未能启动，请检查网络后重试。";
          setButtonsBusy([install], false);
        }
      });
      item.append(install);
    }
    if (canUninstall && state !== "downloading" && !runtimePreparing) {
      const uninstall = button("卸载视频组件", async () => {
        if (!await confirmAction("确认卸载视频回信的全部本地组件？已生成的视频、信件和记忆会保留。")) return;
        if (!await confirmAction("重新启用视频回信需要再次下载或导入约 36.8 GiB，仍要继续吗？")) return;
        setButtonsBusy([uninstall], true);
        result.textContent = "正在卸载视频组件……";
        try {
          const response = await requestCapability(
            VIDEO_CAPABILITY_ACTION_PATH,
            { action: "uninstall" },
            2 * 60 * 1000
          );
          if (response.status === "REJECTED") {
            result.textContent = "视频组件正在使用，请等待当前任务结束后重试。";
            setButtonsBusy([uninstall], false);
            return;
          }
          await renderVideoCapabilityPanel(panel);
        } catch (_error) {
          result.textContent = "视频组件卸载失败，请关闭正在运行的视频任务后重试。";
          setButtonsBusy([uninstall], false);
        }
      });
      item.append(uninstall);
    }
    item.append(result);
    const list = stack();
    list.append(item);
    const refresh = actions();
    const refreshButton = button("重新检测", async () => {
      refreshButton.textContent = "检测中…";
      setButtonsBusy([refreshButton], true);
      await renderVideoCapabilityPanel(panel);
      panel.append(text(
        "p",
        "重新检测完成，上方已显示最新结果。",
        "text-text-secondary text-caption-m font-regular"
      ));
    });
    refresh.append(refreshButton);
    panel.replaceChildren(heading, summary, list, refresh);
    if (state === "downloading" || runtimePreparing) {
      const progressStartedAt = Date.now();
      const updateProgress = async () => {
        let nextPayload = null;
        try {
          nextPayload = await requestJson(VIDEO_CAPABILITY_PATH);
        } catch (_error) {
          nextPayload = null;
        }
        if (panel.videoCapabilityGeneration !== renderGeneration) return;
        if (!nextPayload) {
          panel.videoCapabilityProgressTimer = window.setTimeout(updateProgress, 1000);
          return;
        }
        const nextBundles = nextPayload && Array.isArray(nextPayload.bundles)
          ? nextPayload.bundles
          : [];
        const nextState = videoCapabilityViewState(nextBundles).state;
        const nextRuntime = nextPayload && nextPayload.runtime_import;
        const nextRuntimePreparing = nextRuntime && ["queued", "extracting", "checking", "testing"].includes(nextRuntime.state);
        if (nextState !== "downloading" && !nextRuntimePreparing) {
          await renderVideoCapabilityPanel(panel);
          return;
        }
        const nextDownloadedBytes = nextBundles.reduce(
          (total, entry) => total + (Number(entry.downloaded_bytes) || 0),
          0
        );
        const nextTotalBytes = nextBundles.reduce(
          (total, entry) => total + (Number(entry.total_bytes) || 0),
          0
        );
        const elapsedSeconds = Math.max(0, Math.floor((Date.now() - progressStartedAt) / 1000));
        const activeFile = nextBundles.find((entry) =>
          ["queued", "downloading", "verifying"].includes(entry.state)
          && typeof entry.current_file === "string"
        );
        const activeFileText = activeFile ? `，当前：${activeFile.current_file}` : "";
        progressText.textContent = nextTotalBytes
          ? `已处理 ${formatBytes(nextDownloadedBytes)} / ${formatBytes(nextTotalBytes)}，已用时 ${elapsedSeconds} 秒${activeFileText}`
          : `大小将在安装时按固定清单校验，已用时 ${elapsedSeconds} 秒${activeFileText}`;
        if (nextRuntimePreparing) {
          const checkedBytes = Math.max(0, Number(nextRuntime.checked_bytes) || 0);
          const runtimeTotalBytes = Math.max(0, Number(nextRuntime.total_bytes) || 0);
          progressText.textContent = runtimeTotalBytes > 0
            ? `正在准备运行环境：${formatBytes(checkedBytes)} / ${formatBytes(runtimeTotalBytes)}，已用时 ${elapsedSeconds} 秒`
            : `正在准备视频运行环境，已用时 ${elapsedSeconds} 秒……`;
        }
        panel.videoCapabilityProgressTimer = window.setTimeout(updateProgress, 1000);
      };
      panel.videoCapabilityProgressTimer = window.setTimeout(updateProgress, 1000);
    }
  };

  const renderCapabilityPanel = async (panel) => {
    const heading = text("h3", "本地能力与下载", "text-text-title text-title-m");
    const summary = text(
      "p",
      "已有自动安装的能力可直接下载；其他能力保留国内源优先、官方源备用。",
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
      "下载我们发布的 .oliviapatch 后，可在这里增量更新，无需重装完整运行包。",
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
    const choose = button("选择已下载的补丁", async () => {
      setButtonsBusy([choose], true);
      try {
        const payload = await requestUpdate({ action: "select" });
        if (payload.status === "SELECTED" && typeof payload.package_path === "string") {
          packagePath = payload.package_path;
          selectedPatch.textContent = `已选择：${packagePath.split(/[\\/]/).pop()}`;
          result.textContent = "";
        }
      } catch (error) {
        result.textContent = `无法选择补丁：${error && error.code ? error.code : "UPDATE_PICKER_UNAVAILABLE"}`;
      } finally {
        setButtonsBusy([choose], false);
      }
    });
    const install = button("安装本地补丁", async () => {
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
    controls.append(choose, install, rollback);
    panel.replaceChildren(
      heading,
      summary,
      selectedPatch,
      digest.wrapper,
      controls,
      result
    );
  };

  const loadDialogData = async (statusNode, panels, initialMode) => {
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
      statusNode.textContent = "本机陪伴服务已连接。";
      statusNode.dataset.state = "available";
      await Promise.allSettled(tasks.concat([
        renderMemoryPanel(panels.memory, capabilities.memory),
        renderPrivateWorldPanel(panels.privateWorld, capabilities.private_world),
      ]));
    } catch (_error) {
      statusNode.textContent = "本机陪伴服务暂不可用。";
      statusNode.dataset.state = "unavailable";
      renderUnavailable(panels.memory, "unavailable", "长期记忆");
      renderUnavailable(panels.privateWorld, "unavailable", "私人世界");
    }
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
  const mountMainNavigation = () => {
    const route = window.location.hash.split("?")[0];
    let nav = document.querySelector("[data-olivia-main-navigation]");
    if (route !== "#/studio" && route !== "#/collection") {
      nav?.remove();
      return;
    }
    if (!nav) {
      nav = document.createElement("nav");
      nav.setAttribute("data-olivia-main-navigation", "");
      nav.setAttribute("aria-label", "信箱与曲库");
      Object.assign(nav.style, {
        position: "fixed", top: "60px", left: "120px", zIndex: "20",
        display: "flex", gap: "8px", WebkitAppRegion: "no-drag",
      });
      for (const [label, href] of [["信箱", "#/collection"], ["曲库", "#/studio"]]) {
        const link = text("a", label, "text-body-m");
        link.href = href;
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
    for (const link of nav.querySelectorAll("a")) {
      const active = link.getAttribute("href") === route;
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
    const close = button(initialMode ? "稍后设置" : "关闭", async () => {
      if (initialMode) {
        try {
          await finishInitialSetup(true);
        } catch (_error) {
          return;
        }
      }
      backdrop.remove();
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
          { id: "capability", label: "本地能力与下载", key: "capability" },
          { id: "update", label: "补丁更新", key: "update" },
          { id: "memory", label: "长期记忆", key: "memory" },
          { id: "private-world", label: "私人世界", key: "privateWorld" },
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
        backdrop.remove();
      }
    });
    backdrop.addEventListener("keydown", (event) => {
      if (!initialMode && event.key === "Escape") {
        backdrop.remove();
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

  const mountVideoReplySetting = (section) => {
    const row = document.createElement("div");
    row.className = "flex items-center justify-between px-0 py-3 rounded-3";
    const copy = document.createElement("div");
    copy.className = "flex flex-col gap-0 flex-1 min-w-0";
    const state = text("div", "正在检测视频运行环境，第一次可能需要几分钟…", "text-text-secondary text-caption-m font-regular");
    state.setAttribute("aria-live", "polite");
    copy.append(text("div", "允许视频回信", "text-text-body text-label-l"), text("div", "已接收的信件不会因设置变化被取消。", "text-text-secondary text-body-m font-regular"), state);
    let enabled = null;
    let ready = false;
    let missingDependencies = [];
    let message = "正在检测视频运行环境，第一次可能需要几分钟…";
    let toggle = null;
    let downloads = null;
    const render = () => {
      if (!toggle || !downloads) return;
      const settingAvailable = typeof enabled === "boolean";
      toggle.disabled = !settingAvailable || (!ready && !enabled);
      toggle.textContent = settingAvailable ? (enabled ? "已开启" : "已关闭") : "暂不可用";
      toggle.setAttribute("aria-pressed", settingAvailable ? String(enabled) : "false");
      downloads.textContent = ready ? "管理下载" : "下载缺失组件";
      state.textContent = message;
    };
    const hydrate = async () => {
      try {
        const payload = await requestJson(VIDEO_REPLY_SETTINGS_PATH);
        if (payload.state !== "available") throw new Error("setting-unavailable");
        enabled = payload.enabled;
        ready = payload.ready === true;
        const byId = VIDEO_REPLY_DEPENDENCY_LABELS;
        missingDependencies = Array.isArray(payload.dependencies)
          ? payload.dependencies
            .filter((item) => item && item.state !== "ready" && byId.has(item.id))
            .map((item) => byId.get(item.id))
          : [];
        const voiceReference = Array.isArray(payload.dependencies)
          ? payload.dependencies.find((item) => item && item.id === "voice_reference")
          : null;
        const voiceNeedsPrivateRepair = voiceReference
          && voiceReference.install_mode === "managed"
          && (
            voiceReference.reason_code === "VOICE_REFERENCE_UNAVAILABLE"
            || voiceReference.reason_code === "VOICE_REFERENCE_INVALID"
          );
        message = voiceNeedsPrivateRepair
          ? `受管林离音色不可用（${byId.get("voice_reference")}），请重新运行提供此私有版本的安装程序修复。`
          : !ready && enabled
          ? `视频回信偏好已开启，但当前缺少依赖，不会生效：${missingDependencies.join("、") || "请检查本地能力"}`
          : !ready
          ? `缺少依赖，无法开启视频回信：${missingDependencies.join("、") || "请检查本地能力"}`
          : enabled
          ? "新信默认可参与视频路由。"
          : "新信将直接使用文字回信。";
      } catch (_error) {
        enabled = null;
        ready = false;
        missingDependencies = [];
        message = "设置暂不可用，已安全禁用。";
      }
      render();
    };
    const apply = async () => {
      if (!toggle || typeof enabled !== "boolean") return;
      const previous = enabled;
      setButtonsBusy([toggle], true);
      try {
        const payload = await requestMutation(VIDEO_REPLY_SETTINGS_PATH, { enabled: !previous, request_id: videoReplyRequestId() });
        if (!["APPLIED", "NOOP", "DUPLICATE"].includes(payload.status) || typeof payload.enabled !== "boolean") throw new Error("mutation-unavailable");
        enabled = payload.enabled;
        message = enabled ? "新信默认可参与视频路由。" : "新信将直接使用文字回信。";
      } catch (error) {
        enabled = previous;
        if (error && error.code === "VIDEO_REPLY_DEPENDENCIES_MISSING") {
          const byId = VIDEO_REPLY_DEPENDENCY_LABELS;
          const labels = (error.missingDependencies || []).flatMap((id) => byId.has(id) ? [byId.get(id)] : []);
          message = `缺少依赖，无法开启视频回信：${labels.join("、") || "请检查本地能力"}`;
        } else {
          message = error && error.code === "VIDEO_REPLY_SETTING_REQUEST_CONFLICT" ? "设置请求冲突，原设置保持不变。" : "设置暂不可用，原设置保持不变。";
        }
      } finally {
        setButtonsBusy([toggle], false);
        render();
      }
    };
    toggle = button("已开启", apply);
    toggle.setAttribute("aria-label", "切换视频回信");
    downloads = button("下载缺失组件", () => openDialog(false, "capability"));
    const controls = actions();
    controls.append(toggle, downloads);
    row.append(copy, controls);
    section.append(text("div", "视频回信", "text-text-body text-title-m"), row);
    render();
    void hydrate();
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
        const payload = await requestMutation(LOCAL_LETTER_IMPORT_PATH, {});
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
            : "本地信件导入失败，请重启 Olivia 后重试。";
        importButton.textContent = "重试导入";
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
      text("div", "记忆与私人世界", "text-text-body text-label-l"),
      text(
        "div",
        "在 Olivia 客户端内查看并管理本地连续性。",
        "text-text-secondary text-body-m font-regular"
      )
    );

    row.append(copy, button("打开", () => openDialog(false)));
    section.append(title, row);
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
        const textareas = dialog.querySelectorAll("textarea");
        const titles = Array.from(
          dialog.querySelectorAll('h1,h2,h3,[class*="title"]')
        ).filter((item) => item.textContent.trim() === LETTER_COMPOSER_TITLE);
        const submitButtons = Array.from(
          dialog.querySelectorAll("button")
        ).filter((item) => item.textContent.trim() === LETTER_SUBMIT_LABEL);
        return textareas.length === 1 && titles.length === 1 && submitButtons.length === 1;
      }).map((dialog) => dialog.querySelector("textarea"))
    );
    if (matches.size !== 1) {
      return;
    }
    matches.values().next().value.maxLength = LETTER_CHARACTER_LIMIT;
  };

  const schedule = () => {
    if (scheduled) {
      return;
    }
    scheduled = true;
    window.requestAnimationFrame(() => {
      scheduled = false;
      constrainLetterInputs();
      mountMainNavigation();
      mountShell();
      maybeOpenInitialSetup();
    });
  };

  const observer = new MutationObserver(schedule);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("hashchange", schedule);
  window.addEventListener("popstate", schedule);
  if (typeof window.setInterval === "function") {
    setupPoll = window.setInterval(maybeOpenInitialSetup, 1500);
  }
  schedule();
})();
'''


__all__ = ["BOOTSTRAP_JAVASCRIPT", "SETTINGS_UI_VERSION"]
