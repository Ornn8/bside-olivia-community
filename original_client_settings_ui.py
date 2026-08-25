"""Self-contained script injected into the original Olivia settings surface."""

from __future__ import annotations


SETTINGS_UI_VERSION = "p03.original-settings-manage.v1"

BOOTSTRAP_JAVASCRIPT = r'''(() => {
  "use strict";

  const loader = document.currentScript;
  const rawApiBase = loader && loader.dataset ? loader.dataset.apiBase : "";
  const ROOT_ATTR = "data-olivia-companion-settings-root";
  const DIALOG_ATTR = "data-olivia-companion-settings-dialog";
  const STATUS_PATH = "/toy/companion/status";
  const MEMORY_PATH = "/toy/companion/memory";
  const PRIVATE_WORLD_PATH = "/toy/companion/private-world";
  const CANDIDATES_PATH = "/toy/companion/private-world/candidates";
  const MEMORY_CORRECT_PATH = "/toy/companion/memory/correct";
  const MEMORY_DELETE_PATH = "/toy/companion/memory/delete";
  const MEMORY_PAUSE_PATH = "/toy/companion/memory/pause";
  const MEMORY_RESUME_PATH = "/toy/companion/memory/resume";
  const MEMORY_CLEAR_PATH = "/toy/companion/memory/clear";
  const CONFIRM_HEADER = "X-Olivia-Companion-Action";
  const CONFIRM_VALUE = "confirmed";
  const LETTER_CHARACTER_LIMIT = 1200;
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

  const card = () => {
    const element = document.createElement("article");
    element.style.padding = "14px";
    element.style.borderRadius = "10px";
    element.style.background = "var(--el-fill-color-light, rgba(0,0,0,0.035))";
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

  const levelLabels = {
    unknown: "未知",
    low: "低",
    medium: "中",
    high: "高",
  };

  const stageLabels = {
    unknown: "尚未确认",
    acquaintance: "认识",
    familiar: "熟悉",
    close: "亲近",
  };

  const homeLabels = {
    no_access: "未授权",
    visit_access: "可到访",
    errand_access: "可代办日常事项",
    domestic_access: "可参与居家日常",
  };

  const awarenessLabels = {
    control_only: "仅本机记录",
    pending: "等待确认",
    character_known: "林离已经知道",
  };

  const candidateLabels = {
    boundary_respected: "边界被尊重",
    conflict: "发生冲突",
    repair: "完成修复",
  };

  const capabilityState = (value) => {
    const state = value && typeof value.state === "string" ? value.state : "unavailable";
    return Object.hasOwn(stateLabels, state) ? state : "unavailable";
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

  const requestJson = async (path, params = {}) => {
    const endpoint = new URL(path, apiBase);
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== "") {
        endpoint.searchParams.set(key, String(value));
      }
    }
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(endpoint, {
        method: "GET",
        cache: "no-store",
        credentials: "omit",
        headers: { "Accept": "application/json" },
        signal: controller.signal,
      });
      const payload = await response.json();
      if (!response.ok || !payload || !["READY", "PAUSED"].includes(payload.status)) {
        throw new Error("unavailable");
      }
      return payload;
    } finally {
      window.clearTimeout(timeout);
    }
  };

  const requestMutation = async (path, body) => {
    const endpoint = new URL(path, apiBase);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 8000);
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
      let payload = null;
      try {
        payload = await response.json();
      } catch (_error) {
        payload = null;
      }
      if (!response.ok || !payload || typeof payload.status !== "string") {
        const error = new Error("mutation-unavailable");
        error.code = payload && typeof payload.error_code === "string"
          ? payload.error_code
          : "COMPANION_MUTATION_UNAVAILABLE";
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
            if (!window.confirm("确认用新内容替换这条长期记忆？")) {
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
          if (!window.confirm("确认删除这条长期记忆？原始信件不会被删除。")) {
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

  const renderMemoryPanel = async (panel, capability) => {
    const state = capabilityState(capability);
    if (state === "disabled" || state === "unavailable") {
      renderUnavailable(panel, state, "长期记忆");
      return;
    }

    const heading = text("h3", "长期记忆", "text-text-title text-title-m");
    const paused = capability && capability.reason_code === "MEMORY_ADMIN_PAUSED";
    const summary = text(
      "p",
      `状态：${paused ? "已暂停（不检索、不写入）" : stateLabels[state]}${Number.isInteger(capability.count) ? `，共 ${capability.count} 条` : ""}`,
      "text-text-secondary text-body-m font-regular"
    );
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
    const toggle = button(paused ? "恢复长期记忆" : "暂停长期记忆", async () => {
      const action = paused ? "恢复" : "暂停";
      if (!window.confirm(`确认${action} Mem0 长期记忆？Archive 和私人世界不会受影响。`)) {
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
        await load();
      } catch (_error) {
        resultState.textContent = `长期记忆${action}失败。`;
      } finally {
        setButtonsBusy([toggle, clear], false);
      }
    });
    const clear = button("清空所有长期记忆", async () => {
      if (!window.confirm("这会清空所有 Mem0 长期记忆；Archive 和私人世界不会被删除。是否继续？")) {
        return;
      }
      if (window.prompt("请输入“清空”以作第二次确认。") !== "清空") {
        resultState.textContent = "未完成第二次确认，未清空长期记忆。";
        return;
      }
      setButtonsBusy([toggle, clear], true);
      resultState.textContent = "正在清空长期记忆……";
      try {
        const payload = await requestMutation(MEMORY_CLEAR_PATH, {
          request_id: requestId("memory.clear"),
          reason: "用户在原版 Olivia 设置中二次确认清空 Mem0 长期记忆。",
          confirmed: true,
        });
        resultState.textContent = mutationMessage(payload, "长期记忆已清空；Archive 和私人世界未改动。");
        await load();
      } catch (_error) {
        resultState.textContent = "清空长期记忆失败，Archive 和私人世界未改动。";
      } finally {
        setButtonsBusy([toggle, clear], false);
      }
    });
    lifecycleControls.append(toggle, clear);
    panel.replaceChildren(heading, summary, lifecycleControls, controls, resultState, list);
    await load();
  };

  const renderPrivateSummary = (container, payload) => {
    const levels = payload && payload.levels && typeof payload.levels === "object"
      ? payload.levels
      : {};
    const nicknames = Array.isArray(payload.nickname_permissions)
      ? payload.nickname_permissions.filter((value) => typeof value === "string")
      : [];
    container.append(
      field("关系阶段", stageLabels[payload.relationship_stage] || "尚未确认"),
      field("熟悉程度", levelLabels[levels.familiarity] || "未知"),
      field("信任", levelLabels[levels.trust] || "未知"),
      field("自在程度", levelLabels[levels.comfort] || "未知"),
      field("亲近程度", levelLabels[levels.closeness] || "未知"),
      field("紧张程度", levelLabels[levels.tension] || "未知"),
      field("私人称呼", nicknames.length ? nicknames.join("、") : "未授权"),
      field("住所权限", homeLabels[payload.home_access] || "未授权")
    );

    const continuationTitle = text(
      "h4",
      "本地世界线",
      "text-text-title text-label-l font-medium"
    );
    continuationTitle.style.marginTop = "8px";
    container.append(continuationTitle);
    const facts = Array.isArray(payload.continuation_facts)
      ? payload.continuation_facts
      : [];
    if (!facts.length) {
      container.append(
        text("p", "暂无本地世界线记录。", "text-text-secondary text-body-m font-regular")
      );
      return;
    }
    const factList = stack();
    for (const fact of facts) {
      if (!fact || typeof fact.statement !== "string") {
        continue;
      }
      const item = card();
      item.append(
        text("p", fact.statement, "text-text-body text-body-m font-regular"),
        text(
          "p",
          awarenessLabels[fact.awareness] || "状态未知",
          "text-text-secondary text-caption-m font-regular"
        )
      );
      factList.append(item);
    }
    container.append(factList);
  };

  const renderCandidateList = (
    container,
    payload,
    capability,
    reload,
    resultState
  ) => {
    const state = capabilityState(capability);
    const title = text(
      "h4",
      "待确认的关系建议",
      "text-text-title text-label-l font-medium"
    );
    title.style.marginTop = "14px";
    container.append(title);
    if (state === "disabled" || state === "unavailable") {
      container.append(
        text(
          "p",
          `关系建议${state === "disabled" ? "未启用。" : "暂时不可用。"}`,
          "text-text-secondary text-body-m font-regular"
        )
      );
      return;
    }
    const candidates = payload && Array.isArray(payload.candidates)
      ? payload.candidates
      : [];
    if (!candidates.length) {
      container.append(
        text("p", "暂无待确认建议。", "text-text-secondary text-body-m font-regular")
      );
      return;
    }
    const list = stack();
    for (const candidate of candidates) {
      if (!candidate || typeof candidate.summary !== "string") {
        continue;
      }
      const item = card();
      item.append(
        text(
          "h5",
          candidateLabels[candidate.candidate_type] || "关系建议",
          "text-text-title text-label-l font-medium"
        ),
        text("p", candidate.summary, "text-text-body text-body-m font-regular")
      );
      const created = formatTime(candidate.created_at);
      if (created) {
        item.append(
          text("p", created, "text-text-secondary text-caption-m font-regular")
        );
      }

      if (typeof candidate.candidate_id === "string" && candidate.candidate_id) {
        const controls = actions();
        const decide = async (decision, approve, reject) => {
          const actionLabel = decision === "approve" ? "批准" : "拒绝";
          const explanation = decision === "approve"
            ? "批准后，这条建议会通过现有 PrivateWorld 命令服务写入关系记录。"
            : "拒绝后，关系状态不会发生变化。";
          if (!window.confirm(`${explanation}\n\n确认${actionLabel}这条建议？`)) {
            return;
          }
          setButtonsBusy([approve, reject], true);
          resultState.textContent = `正在${actionLabel}关系建议……`;
          const path = `${CANDIDATES_PATH}/${encodeURIComponent(candidate.candidate_id)}/${decision}`;
          try {
            const mutation = await requestMutation(path, {
              request_id: requestId(`candidate.${decision}`),
              reason: decision === "approve"
                ? "用户在原版 Olivia 设置中明确批准关系建议。"
                : "用户在原版 Olivia 设置中明确拒绝关系建议。",
              decided_at: new Date().toISOString(),
            });
            await reload();
            resultState.textContent = mutationMessage(
              mutation,
              decision === "approve" ? "关系建议已批准。" : "关系建议已拒绝。"
            );
          } catch (_error) {
            resultState.textContent = "关系建议处理失败，关系状态保持不变。";
          } finally {
            setButtonsBusy([approve, reject], false);
          }
        };
        const approve = button("批准", () => decide("approve", approve, reject));
        const reject = button("拒绝", () => decide("reject", approve, reject));
        controls.append(approve, reject);
        item.append(controls);
      }
      list.append(item);
    }
    container.append(list);
  };

  const renderPrivateWorldPanel = async (panel, privateCapability, candidateCapability) => {
    const privateState = capabilityState(privateCapability);
    const heading = text("h3", "私人世界", "text-text-title text-title-m");
    const summary = text(
      "p",
      `状态：${stateLabels[privateState]}`,
      "text-text-secondary text-body-m font-regular"
    );
    const resultState = text(
      "p",
      "正在读取私人世界……",
      "text-text-secondary text-body-m font-regular"
    );
    resultState.setAttribute("aria-live", "polite");
    const content = stack();
    panel.replaceChildren(heading, summary, resultState, content);

    const load = async () => {
      content.replaceChildren();
      resultState.textContent = "正在读取私人世界……";
      if (privateState !== "disabled" && privateState !== "unavailable") {
        try {
          const privatePayload = await requestJson(PRIVATE_WORLD_PATH);
          renderPrivateSummary(content, privatePayload);
        } catch (_error) {
          content.append(
            text("p", "私人世界暂时无法读取。", "text-text-secondary text-body-m font-regular")
          );
        }
      } else {
        content.append(
          text(
            "p",
            `私人世界${privateState === "disabled" ? "未启用。" : "暂时不可用。"}`,
            "text-text-secondary text-body-m font-regular"
          )
        );
      }

      let candidatePayload = null;
      const candidateState = capabilityState(candidateCapability);
      if (candidateState !== "disabled" && candidateState !== "unavailable") {
        try {
          candidatePayload = await requestJson(CANDIDATES_PATH, { limit: 50 });
        } catch (_error) {
          candidatePayload = null;
        }
      }
      renderCandidateList(
        content,
        candidatePayload,
        candidateCapability,
        load,
        resultState
      );
      resultState.textContent = "已读取本机私人世界。";
    };

    await load();
  };

  const loadDialogData = async (statusNode, panels) => {
    statusNode.textContent = "正在连接本机陪伴服务……";
    try {
      const payload = await requestJson(STATUS_PATH);
      const capabilities = payload.capabilities && typeof payload.capabilities === "object"
        ? payload.capabilities
        : {};
      statusNode.textContent = "本机陪伴服务已连接。";
      statusNode.dataset.state = "available";
      await Promise.allSettled([
        renderMemoryPanel(panels.memory, capabilities.memory),
        renderPrivateWorldPanel(
          panels.privateWorld,
          capabilities.private_world,
          capabilities.candidates
        ),
      ]);
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
    document.querySelector(`[${DIALOG_ATTR}]`)?.remove();
  };

  const openDialog = () => {
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
    dialog.style.background = "var(--el-bg-color-overlay, var(--el-bg-color, #ffffff))";
    dialog.style.boxShadow = "0 24px 80px rgba(0, 0, 0, 0.45)";
    dialog.style.color = "var(--el-text-color-primary, #303133)";
    dialog.style.pointerEvents = "auto";
    dialog.style.webkitAppRegion = "no-drag";

    const theme = document.createElement("style");
    theme.textContent = `
      [${DIALOG_ATTR}] [role="dialog"] .text-text-title,
      [${DIALOG_ATTR}] [role="dialog"] .text-text-body {
        color: var(--el-text-color-primary, #303133) !important;
      }
      [${DIALOG_ATTR}] [role="dialog"] .text-text-secondary {
        color: var(--el-text-color-secondary, #606266) !important;
      }
      [${DIALOG_ATTR}] [role="dialog"] button {
        color: var(--el-text-color-primary, #303133) !important;
        border-color: var(--el-border-color, #dcdfe6) !important;
      }
      [${DIALOG_ATTR}] [role="dialog"] button,
      [${DIALOG_ATTR}] [role="dialog"] input,
      [${DIALOG_ATTR}] [role="dialog"] textarea {
        -webkit-app-region: no-drag !important;
        pointer-events: auto !important;
      }
    `;

    const header = document.createElement("div");
    header.style.display = "flex";
    header.style.alignItems = "center";
    header.style.justifyContent = "space-between";
    header.style.gap = "24px";

    const heading = text("h2", "本地陪伴", "text-text-title text-headline-m");
    heading.id = "olivia-companion-dialog-title";
    heading.style.margin = "0";
    const close = button("关闭", () => backdrop.remove());
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
    const definitions = [
      { id: "memory", label: "长期记忆", key: "memory" },
      { id: "private-world", label: "私人世界", key: "privateWorld" },
    ];

    const showPanel = (id) => {
      for (const tab of tabs.querySelectorAll('[role="tab"]')) {
        const active = tab.dataset.panelId === id;
        tab.setAttribute("aria-selected", active ? "true" : "false");
        tab.style.background = active
          ? "var(--el-fill-color, rgba(0,0,0,0.06))"
          : "transparent";
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
      panel.style.background = "var(--el-fill-color-light, rgba(0,0,0,0.035))";
      panel.style.display = "grid";
      panel.style.gap = "14px";
      panel.append(
        text("h3", definition.label, "text-text-title text-title-m"),
        text("p", "正在读取……", "text-text-secondary text-body-m font-regular")
      );
      panelNodes[definition.key] = panel;
      panels.append(panel);
    }

    dialog.append(header, status, tabs, panels);
    backdrop.append(theme, dialog);
    backdrop.addEventListener("click", (event) => {
      if (event.target === backdrop) {
        backdrop.remove();
      }
    });
    backdrop.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        backdrop.remove();
      }
    });
    document.body.append(backdrop);
    showPanel("memory");
    close.focus();
    loadDialogData(status, panelNodes);
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

    row.append(copy, button("打开", openDialog));
    section.append(title, row);
    container.append(section);
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
      mountShell();
    });
  };

  const observer = new MutationObserver(schedule);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("hashchange", schedule);
  window.addEventListener("popstate", schedule);
  schedule();
})();
'''


__all__ = ["BOOTSTRAP_JAVASCRIPT", "SETTINGS_UI_VERSION"]
