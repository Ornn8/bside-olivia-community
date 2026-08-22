import {ControlAPIError, establishSession, get, logout, post} from "./api.js";

const labels = {
  unknown: "未知",
  low: "低",
  medium: "中",
  high: "高",
  acquaintance: "相识",
  familiar: "熟悉",
  close: "亲近",
  no_access: "无访问权限",
  visit_access: "拜访权限",
  errand_access: "代办权限",
  domestic_access: "共同生活权限",
  control_only: "仅系统知道",
  pending: "待确认",
  character_known: "林离已知",
  record_boundary_respected: "边界被尊重",
  record_conflict: "冲突",
  record_repair: "修复",
  confirm_relationship_stage: "关系阶段确认",
};

const byId = (id) => document.getElementById(id);
const notice = byId("notice");

function label(value) {
  return labels[value] || value || "—";
}

function requestId(prefix) {
  const suffix = globalThis.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}.${suffix}`;
}

function common(prefix, reason) {
  return {
    request_id: requestId(prefix),
    occurred_at: new Date().toISOString(),
    reason,
    evidence_refs: [],
  };
}

function showNotice(message, failed = false) {
  notice.textContent = message;
  notice.dataset.state = failed ? "error" : "ok";
}

function clearChildren(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

function renderSnapshot(snapshot) {
  byId("relationship-stage").textContent = label(snapshot.relationship_stage);
  for (const [name, value] of Object.entries(snapshot.levels)) {
    byId(`level-${name}`).textContent = label(value);
  }
  byId("home-access").textContent = label(snapshot.home_access);
  byId("home-select").value = snapshot.home_access;
  byId("nickname-summary").textContent = snapshot.nickname_permissions.length
    ? snapshot.nickname_permissions.join("、")
    : "未授权";

  const nicknameList = byId("nickname-list");
  clearChildren(nicknameList);
  for (const nickname of snapshot.nickname_permissions) {
    const item = document.createElement("li");
    item.textContent = nickname;
    nicknameList.append(item);
  }
  if (!snapshot.nickname_permissions.length) {
    const item = document.createElement("li");
    item.textContent = "暂无已授权称呼";
    nicknameList.append(item);
  }

  const continuationList = byId("continuation-list");
  clearChildren(continuationList);
  for (const fact of snapshot.continuation_facts) {
    const row = document.createElement("tr");
    for (const value of [
      fact.fact_id,
      fact.statement,
      label(fact.awareness),
    ]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    continuationList.append(row);
  }
  if (!snapshot.continuation_facts.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.textContent = "暂无私人世界线事实";
    row.append(cell);
    continuationList.append(row);
  }
}

function renderEvents(events) {
  const timeline = byId("event-list");
  const basisList = byId("stage-basis-list");
  clearChildren(timeline);
  clearChildren(basisList);

  for (const event of [...events].reverse()) {
    const item = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = label(event.command_kind || event.event_type);
    const metadata = document.createElement("span");
    metadata.textContent = `${event.occurred_at} · ${event.reason_code}`;
    const reason = document.createElement("p");
    reason.textContent = event.reason || "系统记录";
    item.append(title, metadata, reason);
    timeline.append(item);
  }
  if (!events.length) {
    const item = document.createElement("li");
    item.textContent = "暂无已确认事件";
    timeline.append(item);
  }

  const allowed = new Set([
    "record_boundary_respected",
    "record_conflict",
    "record_repair",
  ]);
  for (const event of events.filter((row) => allowed.has(row.event_type))) {
    const wrapper = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = "stage-basis";
    input.value = event.event_id;
    wrapper.append(
      input,
      ` ${label(event.event_type)} · ${event.occurred_at}`,
    );
    basisList.append(wrapper);
  }
  if (!basisList.childElementCount) {
    const hint = document.createElement("p");
    hint.textContent = "请先记录至少一个关系事件。";
    basisList.append(hint);
  }
}

async function refresh() {
  const [snapshot, events] = await Promise.all([
    get("/control/api/private-world/snapshot"),
    get("/control/api/private-world/events"),
  ]);
  renderSnapshot(snapshot);
  renderEvents(events.events);
}

async function mutate(path, body, successMessage) {
  await post(path, body);
  await refresh();
  showNotice(successMessage);
}

function selectedBasis() {
  return [
    ...document.querySelectorAll(
      'input[name="stage-basis"]:checked',
    ),
  ].map((node) => node.value);
}

function bindForms() {
  byId("refresh-button").addEventListener("click", async () => {
    try {
      await refresh();
      showNotice("状态已刷新");
    } catch (error) {
      handleError(error);
    }
  });

  byId("relationship-event-form").addEventListener(
    "submit",
    async (event) => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      try {
        await mutate(
          "/control/api/private-world/relationship-events",
          {
            ...common(
              "relationship",
              byId("relationship-reason").value,
            ),
            event_type: form.get("event-type"),
          },
          "关系事件已记录",
        );
        event.currentTarget.reset();
      } catch (error) {
        handleError(error);
      }
    },
  );

  byId("stage-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const basis = selectedBasis();
    if (!basis.length) {
      showNotice("请先选择关系事件依据", true);
      return;
    }
    if (!window.confirm("确认修改关系阶段？这会写入可审计的私人世界记录。")) {
      return;
    }
    try {
      await mutate(
        "/control/api/private-world/relationship-stage",
        {
          ...common("stage", byId("stage-reason").value),
          target_stage: byId("stage-select").value,
          basis_event_ids: basis,
        },
        "关系阶段已确认",
      );
    } catch (error) {
      handleError(error);
    }
  });

  byId("nickname-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await mutate(
        "/control/api/private-world/nicknames",
        {
          ...common("nickname", byId("nickname-reason").value),
          action: byId("nickname-action").value,
          nickname: byId("nickname-value").value,
        },
        "称呼权限已更新",
      );
      event.currentTarget.reset();
    } catch (error) {
      handleError(error);
    }
  });

  byId("home-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!window.confirm("确认修改住所权限？请确保这符合已经建立的私人历史。")) {
      return;
    }
    try {
      await mutate(
        "/control/api/private-world/home-access",
        {
          ...common("home", byId("home-reason").value),
          home_access: byId("home-select").value,
        },
        "住所权限已更新",
      );
    } catch (error) {
      handleError(error);
    }
  });

  byId("continuation-form").addEventListener(
    "submit",
    async (event) => {
      event.preventDefault();
      const action = byId("continuation-action").value;
      const awareness = byId("continuation-awareness").value;
      if (
        awareness === "character_known"
        && !window.confirm(
          "确认让林离知道这条事实？控制层未来信息可能因此进入角色视角。",
        )
      ) {
        return;
      }
      const body = {
        ...common(
          "continuation",
          byId("continuation-reason").value,
        ),
        action,
        fact_id: byId("continuation-id").value,
      };
      if (action === "upsert") {
        body.statement = byId("continuation-statement").value;
        body.awareness = awareness;
      } else if (action === "set_awareness") {
        body.awareness = awareness;
      }
      try {
        await mutate(
          "/control/api/private-world/continuations",
          body,
          "私人世界线已更新",
        );
        event.currentTarget.reset();
      } catch (error) {
        handleError(error);
      }
    },
  );

  byId("logout-button").addEventListener("click", async () => {
    try {
      await logout();
      byId("session-status").textContent = "管理会话已退出";
      showNotice(
        "请从 Windows 开始菜单重新打开 Control Center。",
        true,
      );
    } catch (error) {
      handleError(error);
    }
  });
}

function handleError(error) {
  const code = error instanceof ControlAPIError
    ? error.code
    : "CONTROL_UI_ERROR";
  byId("session-status").textContent = "本地管理不可用";
  showNotice(`操作失败：${code}`, true);
}

async function start() {
  bindForms();
  try {
    const session = await establishSession();
    byId("session-status").textContent = session.csrfAvailable
      ? "本地安全会话已建立"
      : "只读会话；重新从开始菜单打开后可修改";
    await refresh();
    showNotice("PrivateWorld 已加载");
    byId("private-world").focus();
  } catch (error) {
    handleError(error);
  }
}

start();
