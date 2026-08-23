import {ControlAPIError, establishSession, get, logout, post} from "./api.js";

const labels = {
  boundary_respected: "边界被尊重",
  conflict: "冲突",
  repair: "修复",
};

const byId = (id) => document.getElementById(id);
const listNode = byId("candidate-list");
const countNode = byId("candidate-count");
const noticeNode = byId("notice");
const template = byId("candidate-template");

function clearChildren(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

function showNotice(message, failed = false) {
  noticeNode.textContent = message;
  noticeNode.dataset.state = failed ? "error" : "ok";
}

function formatDate(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(parsed);
}

function randomRequestId() {
  const random = globalThis.crypto?.randomUUID?.()
    || Array.from(globalThis.crypto.getRandomValues(new Uint8Array(16)))
      .map((value) => value.toString(16).padStart(2, "0"))
      .join("");
  return `candidate-review.${random}`;
}

function pendingStorageKey(candidateId, decision) {
  return `olivia.control.candidate.${candidateId}.${decision}`;
}

function decisionEnvelope(candidateId, decision, reason) {
  const key = pendingStorageKey(candidateId, decision);
  const stored = sessionStorage.getItem(key);
  if (stored) {
    try {
      const value = JSON.parse(stored);
      if (
        value.reason === reason
        && typeof value.request_id === "string"
        && typeof value.decided_at === "string"
      ) {
        return {key, body: value};
      }
    } catch {
      sessionStorage.removeItem(key);
    }
  }
  const body = {
    request_id: randomRequestId(),
    reason,
    decided_at: new Date().toISOString(),
  };
  sessionStorage.setItem(key, JSON.stringify(body));
  return {key, body};
}

function errorCode(error) {
  return error instanceof ControlAPIError
    ? error.code
    : "CONTROL_CANDIDATE_UI_ERROR";
}

async function decide(candidate, decision, reason, buttons) {
  if (!reason) {
    showNotice("请先填写确认说明。", true);
    return;
  }
  const action = decision === "approve" ? "批准并记录" : "拒绝";
  if (!window.confirm(`确认${action}这条建议？`)) {
    return;
  }

  const envelope = decisionEnvelope(
    candidate.candidate_id,
    decision,
    reason,
  );
  for (const button of buttons) {
    button.disabled = true;
  }
  try {
    const result = await post(
      `/control/api/private-world/candidates/${encodeURIComponent(candidate.candidate_id)}/${decision}`,
      envelope.body,
    );
    sessionStorage.removeItem(envelope.key);
    showNotice(
      result.status === "duplicate"
        ? "这项决定已经记录，没有重复修改关系状态。"
        : decision === "approve"
          ? "建议已批准并写入可审计关系事件。"
          : "建议已拒绝，关系状态没有变化。",
    );
    await refresh();
  } catch (error) {
    showNotice(`操作失败：${errorCode(error)}`, true);
    for (const button of buttons) {
      button.disabled = false;
    }
  }
}

function renderEmpty() {
  const empty = document.createElement("p");
  empty.className = "candidate-empty";
  empty.textContent = "目前没有等待确认的关系事件建议。";
  listNode.append(empty);
}

function renderCandidate(candidate) {
  const fragment = template.content.cloneNode(true);
  const card = fragment.querySelector(".candidate-card");
  const reason = fragment.querySelector(".candidate-reason");
  const approve = fragment.querySelector(".candidate-approve");
  const reject = fragment.querySelector(".candidate-reject");

  card.dataset.candidateId = candidate.candidate_id;
  fragment.querySelector(".candidate-type").textContent = (
    labels[candidate.candidate_type] || candidate.candidate_type
  );
  fragment.querySelector(".candidate-summary").textContent = (
    candidate.summary
  );
  fragment.querySelector(".candidate-confidence").textContent = (
    `建议可信度 ${Math.round(candidate.confidence * 100)}%`
  );
  fragment.querySelector(".candidate-source").textContent = (
    `${candidate.source_letter_id} · 回复版本 ${candidate.source_reply_revision}`
  );
  fragment.querySelector(".candidate-created").textContent = (
    formatDate(candidate.created_at)
  );
  fragment.querySelector(".candidate-expires").textContent = (
    formatDate(candidate.expires_at)
  );

  const buttons = [approve, reject];
  approve.addEventListener("click", () => {
    decide(candidate, "approve", reason.value.trim(), buttons);
  });
  reject.addEventListener("click", () => {
    decide(candidate, "reject", reason.value.trim(), buttons);
  });
  listNode.append(fragment);
}

async function refresh() {
  const data = await get("/control/api/private-world/candidates?limit=100");
  clearChildren(listNode);
  const candidates = Array.isArray(data.candidates) ? data.candidates : [];
  countNode.textContent = `${candidates.length} 条待确认`;
  if (!candidates.length) {
    renderEmpty();
    return;
  }
  for (const candidate of candidates) {
    renderCandidate(candidate);
  }
}

function bindActions() {
  byId("refresh-button").addEventListener("click", async () => {
    try {
      await refresh();
      showNotice("建议列表已刷新。")
    } catch (error) {
      showNotice(`刷新失败：${errorCode(error)}`, true);
    }
  });

  byId("logout-button").addEventListener("click", async () => {
    try {
      await logout();
      window.location.assign("/control/");
    } catch (error) {
      showNotice(`退出失败：${errorCode(error)}`, true);
    }
  });
}

async function start() {
  bindActions();
  try {
    const session = await establishSession();
    if (!session.csrfAvailable) {
      showNotice(
        "当前标签页缺少修改凭据，请从私有世界首页进入本页面。",
        true,
      );
    }
    await refresh();
    byId("pending-title").focus?.();
  } catch (error) {
    showNotice(`加载失败：${errorCode(error)}`, true);
    countNode.textContent = "不可用";
  }
}

start();
