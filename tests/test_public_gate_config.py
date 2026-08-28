from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pytest_ini_is_the_single_public_gate_configuration() -> None:
    config = ConfigParser()
    config.read(ROOT / "pytest.ini", encoding="utf-8")

    configured = {
        line.strip()
        for line in config["pytest"]["testpaths"].splitlines()
        if line.strip()
    }
    required = {
        "tests/http",
        "tests/media",
        "tests/persona",
        "tests/private_world",
        "tests/control_center",
        "tests/installer",
        "tests/memory",
        "tests/test_public_gate_config.py",
    }
    assert required.issubset(configured)
    assert "--import-mode=importlib" in config["pytest"]["addopts"]

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.pytest.ini_options]" not in pyproject


def test_community_policies_publish_actionable_private_reporting_routes() -> None:
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    conduct = (ROOT / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")

    advisory_url = (
        "https://github.com/Ornn8/bside-olivia-community/security/advisories/new"
    )
    contact = "zzhiyuan717@gmail.com"
    assert advisory_url in security
    assert contact in security
    assert contact in conduct
