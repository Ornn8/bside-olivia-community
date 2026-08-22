const statusNode = document.querySelector("#candidate-status");
const detailNode = document.querySelector("#candidate-detail");
const refreshButton = document.querySelector("#refresh-candidates");
const listNode = document.querySelector("#candidate-list");
const emptyNode = document.querySelector("#candidate-empty");
const template = document.querySelector("#candidate-template");

const csrfToken = sessionStorage.getItem("olivia_control_csrf") || "";

const typeLabels = {
  boundary_respected: "边界被尊重",
  conflict: "冲突",
  repair: "修复",
};

const errors = {
  CONTROL_SESSION_REQUIRED: "当前没有管理会话，请从 Olivia Control Center 快捷方式重新打开。",
  CONTROL_SESSION_EXPIRED: "管理会话已过期，请重新打开 Control Center。",
  CONTROL_CSRF_INVALID: "安全校验失败，请重新打开 Control Center。",
  PRIVATE_WORLD_CANDIDATE_CONTROL_UNAVAILABLE: "待确认建议服务暂时不可用。",
  PRIVATE_WORLD_CANDIDATE_EXPIRED: "这条建议已经过期，请刷新列表。",
  PRIVATE_WORLD_CANDIDATE_ALREADY_DECIDED: "这条建议已经处理过，请刷新列表。",
};

function explain(code) {
  return errors[code] || "操作没有完成，请刷新后重试。";
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    ...options,
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch (_error) {
    payload = { error_code: "CONTROL_RESPONSE_INVALID" };
  }
  if (!response.ok) {
    const error = new Error(payload.error_code || "CONTROL_REQUEST_FAILED");
    error.code = payload.error_code || "CONTROL_REQUEST_FAILED";
    throw error;
  }
  return payload;
}

function localDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "未知" : date.toLocaleString();
}

function idempotencyKey(candidateId, decision) {
  const random = crypto.getRandomValues(new Uint32Array(2));
  return `candidate-decision.${candidateId}.${decision}.${random[0].toString(16)}${random[1].toString(16)}`;
}

async function decide(candidate, decision, reason, resultNode, buttons) {
  if (!csrfToken) {
    throw Object.assign(new Error("CONTROL_CSRF_INVALID"), { code: "CONTROL_CSRF_INVALID" });
  }
  buttons.forEach((button) => { button.disabled = true; });
  resultNode.textContent = "正在提交决定……";
  try {
    const payload = await request(
      `/control/api/private-world/candidates/${encodeURIComponent(candidate.candidate_id)}/${decision}`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          idempotency_key: idempotencyKey(candidate.candidate_id, decision),
          reason,
          occurred_at: new Date().toISOString(),
        }),
      },
    );
    resultNode.textContent = payload.status === "approved" ? "已批准并记录。" : "已拒绝这条建议。";
    await loadCandidates();
  } catch (error) {
    resultNode.textContent = explain(error.code);
    buttons.forEach((button) => { button.disabled = false; });
  }
}

function renderCandidate(candidate) {
  const fragment = template.content.cloneNode(true);
  const card = fragment.querySelector(".candidate-card");
  fragment.querySelector(".candidate-type").textContent = typeLabels[candidate.candidate_type] || candidate.candidate_type;
  fragment.querySelector(".candidate-summary").textContent = candidate.summary;
  fragment.querySelector(".candidate-confidence").textContent = `${Math.round(Number(candidate.confidence || 0) * 100)}%`;
  fragment.querySelector(".candidate-source").textContent = `${candidate.source_letter_id} · 回复版本 ${candidate.source_reply_revision}`;
  fragment.querySelector(".candidate-created").textContent = localDate(candidate.created_at);
  fragment.querySelector(".candidate-expires").textContent = localDate(candidate.expires_at);

  const form = fragment.querySelector(".candidate-decision-form");
  const resultNode = fragment.querySelector(".candidate-result");
  const buttons = [...form.querySelectorAll("button[type='submit']")];
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const submitter = event.submitter;
    const decision = submitter?.value;
    const reason = new FormData(form).get("reason")?.toString().trim() || "";
    if (!reason) {
      resultNode.textContent = "请先填写处理说明。";
      return;
    }
    if (!['approve', 'reject'].includes(decision)) {
      resultNode.textContent = "无法识别处理方式。";
      return;
    }
    decide(candidate, decision, reason, resultNode, buttons);
  });
  card.dataset.candidateId = candidate.candidate_id;
  return fragment;
}

async function loadCandidates() {
  statusNode.textContent = "正在读取待确认建议……";
  detailNode.textContent = "";
  refreshButton.disabled = true;
  try {
    const payload = await request("/control/api/private-world/candidates?limit=100");
    const candidates = Array.isArray(payload.candidates) ? payload.candidates : [];
    listNode.replaceChildren(...candidates.map(renderCandidate));
    emptyNode.hidden = candidates.length !== 0;
    statusNode.textContent = candidates.length ? `有 ${candidates.length} 条待确认建议` : "没有待确认建议";
    detailNode.textContent = "批准建议会通过受控命令服务执行；拒绝不会修改关系状态。";
  } catch (error) {
    listNode.replaceChildren();
    emptyNode.hidden = true;
    statusNode.textContent = "无法读取待确认建议";
    detailNode.textContent = explain(error.code);
  } finally {
    refreshButton.disabled = false;
  }
}

refreshButton.addEventListener("click", loadCandidates);
loadCandidates();
