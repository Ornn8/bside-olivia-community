"""The installed backend must contain chat transports, never local credentials."""
from pathlib import Path
import shutil
import subprocess
import zipfile

from installer.component_package import build_component_package
from installer.full_patch import (
    PAYLOAD_REQUIRED_RELATIVE_FILES,
    PAYLOAD_REQUIRED_ROOT_FILES,
    copy_project_payload,
)


def _git(source, *args):
    return subprocess.run(["git", "-C", str(source), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for relative in (*PAYLOAD_REQUIRED_ROOT_FILES, *PAYLOAD_REQUIRED_RELATIVE_FILES):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n" if target.suffix == ".json" else "# fixture\n", encoding="utf-8")
    actual = Path(__file__).parents[2] / "runtime" / "personal_chat"
    target = source / "runtime" / "personal_chat"
    target.mkdir(parents=True)
    modules = sorted(actual.glob("*.py"))
    assert {"backend.py", "service.py", "events.py", "probe.py", "qq.py", "wechat.py"} <= {p.name for p in modules}
    for module in modules:
        shutil.copyfile(module, target / module.name)
    _git(source, "init")
    _git(source, "add", ".")
    for name in ("credentials.json", "wechat.dpapi", "qq.dpapi", "probe.sqlite3"):
        (target / name).write_text("synthetic-private-secret", encoding="utf-8")
    return source, modules


def test_tracked_personal_chat_modules_copied_without_local_credentials(tmp_path):
    source, modules = _source(tmp_path)
    destination = tmp_path / "payload"
    copy_project_payload(source, destination)
    copied = destination / "runtime" / "personal_chat"
    assert {p.name for p in copied.iterdir()} == {p.name for p in modules}
    for module in modules:
        assert (copied / module.name).read_bytes() == module.read_bytes()


def test_component_archive_contains_chat_runtime_without_credentials(tmp_path):
    source, modules = _source(tmp_path)
    _git(source, "-c", "user.name=Payload test", "-c", "user.email=payload@example.invalid",
         "commit", "-m", "Synthetic payload fixture")
    package = tmp_path / "personal-chat.oliviapatch"
    build_component_package(source, package, version="1.1.21-dev-chat",
                            expected_source_commit=_git(source, "rev-parse", "HEAD"))
    with zipfile.ZipFile(package) as archive:
        prefix = "payload/runtime/personal_chat/"
        assert {name[len(prefix):] for name in archive.namelist() if name.startswith(prefix)} == {p.name for p in modules}
        assert all(b"synthetic-private-secret" not in archive.read(name) for name in archive.namelist())
