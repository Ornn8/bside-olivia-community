import json
from pathlib import Path
import tomllib


def test_project_and_installer_publish_the_same_version():
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
    installer = json.loads((root / 'installer/release-version.json').read_text(encoding='utf-8'))
    assert project['project']['version'] == installer['version']
