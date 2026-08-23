from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "original_client_settings_ui.py"
TEST = ROOT / "tests" / "installer" / "test_original_client_settings_ui.py"
WORKFLOW = ROOT / ".github" / "workflows" / "public-smoke.yml"


def replace_once(value: str, old: str, new: str, label: str) -> str:
    if value.count(old) != 1:
        raise RuntimeError(f"SETTINGS_CLICK_FIX_{label}_ANCHOR_INVALID")
    return value.replace(old, new, 1)


def patch_source() -> None:
    value = SOURCE.read_text(encoding="utf-8")
    value = replace_once(
        value,
        '''    element.className = "px-6 py-2.5 rounded-full border border-grey-5 text-text-body text-label-m font-medium cursor-pointer hover:bg-surface-1 transition-colors";\n    element.addEventListener("click", onClick);''',
        '''    element.className = "px-6 py-2.5 rounded-full border border-grey-5 text-text-body text-label-m font-medium cursor-pointer hover:bg-surface-1 transition-colors";\n    element.style.pointerEvents = "auto";\n    element.style.webkitAppRegion = "no-drag";\n    element.addEventListener("click", onClick);''',
        "BUTTON",
    )
    value = replace_once(
        value,
        '''    element.style.background = "rgba(255,255,255,0.06)";\n    element.style.display = "grid";''',
        '''    element.style.background = "var(--el-fill-color-light, rgba(0,0,0,0.035))";\n    element.style.display = "grid";''',
        "CARD_BACKGROUND",
    )
    value = replace_once(
        value,
        '''    backdrop.style.background = "rgba(0, 0, 0, 0.62)";\n\n    const dialog = document.createElement("section");''',
        '''    backdrop.style.background = "rgba(0, 0, 0, 0.62)";\n    backdrop.style.pointerEvents = "auto";\n    backdrop.style.webkitAppRegion = "no-drag";\n\n    const dialog = document.createElement("section");''',
        "BACKDROP",
    )
    value = replace_once(
        value,
        '''    dialog.style.background = "var(--el-bg-color, #202124)";\n    dialog.style.boxShadow = "0 24px 80px rgba(0, 0, 0, 0.45)";\n\n    const header = document.createElement("div");''',
        '''    dialog.style.background = "var(--el-bg-color-overlay, var(--el-bg-color, #ffffff))";\n    dialog.style.boxShadow = "0 24px 80px rgba(0, 0, 0, 0.45)";\n    dialog.style.color = "var(--el-text-color-primary, #303133)";\n    dialog.style.pointerEvents = "auto";\n    dialog.style.webkitAppRegion = "no-drag";\n\n    const theme = document.createElement("style");\n    theme.textContent = `\n      [${DIALOG_ATTR}] [role="dialog"] .text-text-title,\n      [${DIALOG_ATTR}] [role="dialog"] .text-text-body {\n        color: var(--el-text-color-primary, #303133) !important;\n      }\n      [${DIALOG_ATTR}] [role="dialog"] .text-text-secondary {\n        color: var(--el-text-color-secondary, #606266) !important;\n      }\n      [${DIALOG_ATTR}] [role="dialog"] button {\n        color: var(--el-text-color-primary, #303133) !important;\n        border-color: var(--el-border-color, #dcdfe6) !important;\n      }\n      [${DIALOG_ATTR}] [role="dialog"] button,\n      [${DIALOG_ATTR}] [role="dialog"] input,\n      [${DIALOG_ATTR}] [role="dialog"] textarea {\n        -webkit-app-region: no-drag !important;\n        pointer-events: auto !important;\n      }\n    `;\n\n    const header = document.createElement("div");''',
        "DIALOG",
    )
    value = replace_once(
        value,
        '''        tab.setAttribute("aria-selected", active ? "true" : "false");\n        tab.style.background = active ? "rgba(255,255,255,0.12)" : "transparent";\n      }\n      for (const panel of panels.querySelectorAll('[role="tabpanel"]')) {\n        panel.hidden = panel.dataset.panelId !== id;\n      }''',
        '''        tab.setAttribute("aria-selected", active ? "true" : "false");\n        tab.style.background = active\n          ? "var(--el-fill-color, rgba(0,0,0,0.06))"\n          : "transparent";\n      }\n      for (const panel of panels.querySelectorAll('[role="tabpanel"]')) {\n        const active = panel.dataset.panelId === id;\n        panel.hidden = !active;\n        panel.style.display = active ? "grid" : "none";\n      }''',
        "TAB_VISIBILITY",
    )
    value = replace_once(
        value,
        '''      panel.style.background = "rgba(255,255,255,0.06)";\n      panel.style.display = "grid";''',
        '''      panel.style.background = "var(--el-fill-color-light, rgba(0,0,0,0.035))";\n      panel.style.display = "grid";''',
        "PANEL_BACKGROUND",
    )
    value = replace_once(
        value,
        '''    dialog.append(header, status, tabs, panels);\n    backdrop.append(dialog);''',
        '''    dialog.append(header, status, tabs, panels);\n    backdrop.append(theme, dialog);''',
        "THEME_APPEND",
    )
    SOURCE.write_text(value, encoding="utf-8")


def patch_test() -> None:
    value = TEST.read_text(encoding="utf-8")
    value = replace_once(
        value,
        '''    assert "encodeURIComponent" in BOOTSTRAP_JAVASCRIPT\n\n\ndef test_original_settings_management_ui_renders_untrusted_data_as_text_only() -> None:''',
        '''    assert "encodeURIComponent" in BOOTSTRAP_JAVASCRIPT\n    assert 'element.style.pointerEvents = "auto";' in BOOTSTRAP_JAVASCRIPT\n    assert 'element.style.webkitAppRegion = "no-drag";' in BOOTSTRAP_JAVASCRIPT\n    assert 'backdrop.style.pointerEvents = "auto";' in BOOTSTRAP_JAVASCRIPT\n    assert 'dialog.style.pointerEvents = "auto";' in BOOTSTRAP_JAVASCRIPT\n    assert 'panel.style.display = active ? "grid" : "none";' in BOOTSTRAP_JAVASCRIPT\n    assert '-webkit-app-region: no-drag !important;' in BOOTSTRAP_JAVASCRIPT\n    assert 'var(--el-text-color-primary, #303133)' in BOOTSTRAP_JAVASCRIPT\n\n\ndef test_original_settings_management_ui_renders_untrusted_data_as_text_only() -> None:''',
        "TEST",
    )
    TEST.write_text(value, encoding="utf-8")


def restore_workflow() -> None:
    WORKFLOW.write_text(
        '''name: public-smoke\n\non:\n  push:\n  pull_request:\n\npermissions:\n  contents: read\n\njobs:\n  public-smoke:\n    name: Public smoke (Windows / Python 3.12)\n    runs-on: windows-latest\n    timeout-minutes: 15\n    steps:\n      - name: Check out source\n        uses: actions/checkout@v4\n\n      - name: Set up Python\n        uses: actions/setup-python@v5\n        with:\n          python-version: "3.12"\n\n      - name: Install public development dependencies\n        run: python -m pip install -e ".[dev]"\n\n      - name: Run public smoke tests\n        run: python -m pytest -q\n\n      - name: Run repository hardening scan\n        run: python baseline_hardening_scan.py --mode all\n\n      - name: Check whitespace\n        run: git diff --check --exit-code\n''',
        encoding="utf-8",
    )


def main() -> None:
    patch_source()
    patch_test()
    restore_workflow()
    Path(__file__).unlink()


if __name__ == "__main__":
    main()
