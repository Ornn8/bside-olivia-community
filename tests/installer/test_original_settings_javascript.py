from __future__ import annotations

from pathlib import Path
import json
import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_original_settings_bootstrap_has_valid_javascript(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for JavaScript syntax validation")
    source = tmp_path / "olivia-companion-settings.js"
    source.write_text(BOOTSTRAP_JAVASCRIPT, encoding="utf-8")
    completed = subprocess.run(
        [node, "--check", str(source)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_original_settings_actions_remain_bounded_and_in_client() -> None:
    source = BOOTSTRAP_JAVASCRIPT
    assert source.count('method: "POST"') == 2
    assert source.count('method: "GET"') == 1
    assert '"Content-Type": "application/json"' in source
    assert 'const CONFIRM_VALUE = "confirmed"' in source
    assert "window.confirm" not in source
    assert "confirmAction" in source
    assert "window.open" not in source
    assert "innerHTML" not in source
    assert "document.write" not in source
    assert "eval(" not in source
    assert "Function(" not in source
    assert "<iframe" not in source.casefold()
    assert 'method: "DELETE"' not in source
    assert 'method: "PUT"' not in source


def test_confirmation_dialog_can_be_cancelled_by_backdrop_or_escape() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for confirmation behavior validation")
    harness = r'''
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(0, "utf8");
source = source.replace(/\s*schedule\(\);\s*\}\)\(\);\s*$/, `
  globalThis.confirmAction = confirmAction;
})();\n`);

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.style = {};
    this.parent = null;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] || null; }
  addEventListener(name, listener) {
    (this.listeners[name] ||= []).push(listener);
  }
  append(...children) {
    for (const child of children) {
      child.parent = this;
      this.children.push(child);
    }
  }
  remove() {
    if (this.parent) {
      this.parent.children = this.parent.children.filter((child) => child !== this);
    }
  }
  focus() {}
}

const body = new Element("body");
const document = {
  currentScript: { dataset: { apiBase: "http://127.0.0.1:8899/" } },
  body,
  documentElement: body,
  createElement: (tag) => new Element(tag),
  querySelector: () => body.children[body.children.length - 1] || null,
};
const context = {
  URL,
  document,
  MutationObserver: class { observe() {} },
  window: { addEventListener: () => {} },
};
vm.runInNewContext(source, context);

(async () => {
  const backdropPending = context.confirmAction("确认删除？");
  const backdrop = body.children[0];
  const confirmation = backdrop.children[0];
  const message = confirmation.children[0];
  let clickPrevented = false;
  for (const listener of backdrop.listeners.click || []) {
    listener({ target: backdrop, preventDefault: () => { clickPrevented = true; } });
  }
  const backdropResult = await backdropPending;

  const escapePending = context.confirmAction("确认回滚？");
  const escapeBackdrop = body.children[0];
  let escapePrevented = false;
  for (const listener of escapeBackdrop.listeners.keydown || []) {
    listener({ key: "Escape", preventDefault: () => { escapePrevented = true; } });
  }
  const escapeResult = await escapePending;

  process.stdout.write(JSON.stringify({
    backdropResult,
    escapeResult,
    clickPrevented,
    escapePrevented,
    labelledBy: confirmation.getAttribute("aria-labelledby"),
    messageId: message.id || null,
    remainingBackdrops: body.children.length,
  }));
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    completed = subprocess.run(
        [node, "-e", harness],
        input=BOOTSTRAP_JAVASCRIPT.encode("utf-8"),
        capture_output=True,
        timeout=20,
        check=False,
    )
    output = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")
    assert completed.returncode == 0, output
    assert json.loads(completed.stdout) == {
        "backdropResult": False,
        "escapeResult": False,
        "clickPrevented": True,
        "escapePrevented": True,
        "labelledBy": "olivia-companion-confirm-message",
        "messageId": "olivia-companion-confirm-message",
        "remainingBackdrops": 0,
    }


def test_original_settings_reuses_llm_setup_after_login() -> None:
    source = BOOTSTRAP_JAVASCRIPT
    assert 'const SETUP_STATUS_PATH = "/toy/setup/status"' in source
    assert 'const LLM_TEST_PATH = "/toy/setup/llm/test"' in source
    assert 'const LLM_SAVE_PATH = "/toy/setup/llm/save"' in source
    assert 'const SETUP_COMPLETE_PATH = "/toy/setup/complete"' in source
    assert 'const LLM_DELETE_PATH = "/toy/setup/llm/delete"' in source
    assert 'const MEM0_CAPABILITY_PATH = "/toy/capabilities/mem0"' in source
    assert 'const MEM0_CAPABILITY_ACTION_PATH = "/toy/capabilities/mem0/action"' in source
    assert "/toy/capabilities/mem0/import" not in source
    assert "show_initial_setup" in source
    assert "API key" in source
    assert "OpenCode Go" in source
    assert "DeepSeek 官方" in source
    assert "自动选择（国内源优先）" in source
    assert "仅官方源" in source
    assert "导入离线包（暂不可用）" in source
    assert "等待可信签名与受限导入校验完成" in source
    assert 'options.headers[SETUP_SESSION_HEADER] = setupSessionToken' in source
    assert "暂停下载" in source
    assert "继续下载" in source
    assert "约 317 MiB" in source
    assert "无需 GPU" in source
    assert "完成初始设置" in source
    assert "未配置大模型时无法进行真实对话" in source
    assert "可在设置 > 本地陪伴中继续" in source
    assert "剩余" in source
    assert "安装后占用" in source
    assert "实际来源" in source
    assert "精确位置" in source
    assert 'installation_root: "程序目录"' in source
    assert 'local_data_root: "本地数据目录"' in source
    assert "|| isSettingsRoute()" not in source
    assert "innerHTML" not in source


def test_initial_and_later_settings_share_the_complete_optional_capability_panel() -> None:
    source = BOOTSTRAP_JAVASCRIPT

    assert "renderCapabilityPanel(panels.capability)" in source
    assert "renderMem0CapabilityPanel(panels.capability)" not in source
    for label in (
        "长期记忆（Mem0 + BGE）",
        "语音合成（CosyVoice 3）",
        "视频驱动配置（LiveTalking）",
        "口型视频（LatentSync）",
        "音乐生成（MiniMax Music 3）",
        "人声分离（RoFormer）",
        "Olivia 场景与转场素材",
        "媒体工具（FFmpeg）",
        "媒体工作目录",
    ):
        assert label in source
    assert "已有自动安装" in source
    assert "需手动准备" in source
    assert "选择下载源" in source
    assert "打开下载页" in source
    assert "重新检测" in source
    assert 'const VIDEO_REPLY_SOURCE_PATH = "/toy/capabilities/video/source";' in source
    assert "requestMutation(VIDEO_REPLY_SOURCE_PATH" in source
    assert "downloadLink.href" not in source
    assert "CAPABILITY_DOWNLOAD_HOSTS" not in source
    assert "缺少依赖，无法开启视频回信" in source
    assert "missing_dependencies" in source
    assert "toggle.disabled = !settingAvailable || (!ready && !enabled);" in source
    assert 'button("管理下载", () => openDialog(false, "capability"))' in source


def test_initial_setup_dialog_survives_mailbox_route_cleanup() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for JavaScript behavior validation")
    harness = r'''
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(0, "utf8");
source = source.replace(/\}\)\(\);\s*$/, `
  globalThis.mountSettingsShell = mountShell;
})();\n`);

let dialogRemoved = false;
const dialog = { remove: () => { dialogRemoved = true; } };
const document = {
  currentScript: { dataset: { apiBase: "http://127.0.0.1:8899/" } },
  documentElement: {},
  querySelector: (selector) => selector.includes("settings-dialog") ? dialog : null,
  querySelectorAll: () => [],
};
const context = {
  URL, AbortController, document,
  MutationObserver: class { observe() {} },
  window: {
    location: { pathname: "/collection", hash: "" },
    requestAnimationFrame: () => {},
    addEventListener: () => {},
    setInterval: () => 1,
  },
};
vm.runInNewContext(source, context);
context.mountSettingsShell();
process.stdout.write(JSON.stringify({ dialogRemoved }));
'''
    completed = subprocess.run(
        [node, "-e", harness],
        input=BOOTSTRAP_JAVASCRIPT.encode("utf-8"),
        capture_output=True,
        timeout=20,
        check=False,
    )
    output = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")
    assert completed.returncode == 0, output
    assert json.loads(completed.stdout)["dialogRemoved"] is False
