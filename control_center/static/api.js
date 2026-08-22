const CSRF_STORAGE_KEY = "olivia.control.csrf";
let csrfToken = sessionStorage.getItem(CSRF_STORAGE_KEY) || "";

export class ControlAPIError extends Error {
  constructor(code, status) {
    super(code);
    this.code = code;
    this.status = status;
  }
}

async function decode(response) {
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new ControlAPIError("CONTROL_RESPONSE_INVALID", response.status);
  }
  if (!response.ok || payload.ok !== true) {
    throw new ControlAPIError(
      payload?.error?.code || "CONTROL_REQUEST_FAILED",
      response.status,
    );
  }
  return payload.data;
}

async function request(path, {method = "GET", body} = {}) {
  const headers = {Accept: "application/json"};
  const options = {method, headers, credentials: "same-origin"};
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  if (method !== "GET" && method !== "HEAD") {
    if (!csrfToken) {
      throw new ControlAPIError("CONTROL_CSRF_REQUIRED", 403);
    }
    headers["X-CSRF-Token"] = csrfToken;
  }
  return decode(await fetch(path, options));
}

export async function establishSession() {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const bootstrap = fragment.get("bootstrap") || "";
  if (window.location.hash) {
    history.replaceState(null, "", window.location.pathname);
  }
  if (!bootstrap) {
    return {bootstrapped: false, csrfAvailable: Boolean(csrfToken)};
  }
  const response = await fetch("/control/api/session/bootstrap", {
    method: "POST",
    credentials: "same-origin",
    headers: {"Content-Type": "application/json", Accept: "application/json"},
    body: JSON.stringify({token: bootstrap}),
  });
  const data = await decode(response);
  csrfToken = data.csrf_token;
  sessionStorage.setItem(CSRF_STORAGE_KEY, csrfToken);
  return {bootstrapped: true, csrfAvailable: true};
}

export function get(path) {
  return request(path);
}

export function post(path, body) {
  return request(path, {method: "POST", body});
}

export async function logout() {
  if (csrfToken) {
    await post("/control/api/session/logout", {});
  }
  csrfToken = "";
  sessionStorage.removeItem(CSRF_STORAGE_KEY);
}
