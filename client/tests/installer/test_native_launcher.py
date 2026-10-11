from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from installer.build_native_launcher import build_native_launcher
from installer.native_launcher import install_native_launcher

INSTALLER = Path(__file__).parents[2] / "installer"
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native launcher and shell COM")


def powershell(script, *arguments, stdin=None):
    if script[0] == "-Command":
        source = (INSTALLER / "Create-Shortcut.ps1").read_text(encoding="utf-8-sig")
        definition = re.search(r"Add-Type -TypeDefinition @'\n(.*?)\n'@", source, re.S).group(1)
        script = ["-Command", "[Console]::InputEncoding=[Text.Encoding]::UTF8;\n"
                  + "Add-Type -TypeDefinition @'\n" + definition + "\n'@\n" + script[1]]
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           *script, *map(str, arguments)], input=stdin, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def shortcut(path, target=None, arguments=""):
    if target is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        powershell(["-Command", "$p=[Console]::In.ReadToEnd()|ConvertFrom-Json;"
                    "$s=New-Object OliviaShortcut($p.path);"
                    "$s.TargetPath=$p.target;$s.Arguments=$p.arguments;$s.Save()"],
                   stdin=json.dumps(dict(path=str(path), target=str(target), arguments=arguments)))
    result = powershell(["-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                         "$s=New-Object OliviaShortcut([Console]::In.ReadToEnd());"
                         "@{target=$s.TargetPath;arguments=$s.Arguments;cwd=$s.WorkingDirectory;icon=$s.IconLocation}|ConvertTo-Json -Compress"],
                        stdin=str(path))
    return json.loads(result.stdout.lstrip("\ufeff"))


@pytest.fixture(scope="module")
def native(tmp_path_factory):
    directory = tmp_path_factory.mktemp("native-build")
    first = build_native_launcher(INSTALLER, directory / "a/Olivia.exe")
    time.sleep(1.1)
    second = build_native_launcher(INSTALLER, directory / "different path/Olivia.exe")
    assert first.read_bytes() == second.read_bytes()
    return first


@pytest.fixture
def installation(tmp_path, native, monkeypatch):
    root = tmp_path / "测试安装 with spaces"
    root.mkdir()
    shutil.copy2(native, root / "Olivia.exe")
    shutil.copy2(INSTALLER / "assets/olivia.ico", root / "olivia.ico")
    monkeypatch.setenv("OLIVIA_SHORTCUT_DESKTOP", str(tmp_path / "desktop"))
    monkeypatch.setenv("OLIVIA_SHORTCUT_PROGRAMS", str(tmp_path / "programs"))
    return root


def migrate(root, *args):
    return powershell(["-File", str(INSTALLER / "Create-Shortcut.ps1")], "-InstallRoot", root, *args)


def test_reads_shortcut_written_by_legacy_wsh(tmp_path, native, monkeypatch):
    root = tmp_path / "legacy-install"
    root.mkdir()
    shutil.copy2(native, root / "Olivia.exe")
    shutil.copy2(INSTALLER / "assets/olivia.ico", root / "olivia.ico")
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    monkeypatch.setenv("OLIVIA_SHORTCUT_DESKTOP", str(desktop))
    monkeypatch.setenv("OLIVIA_SHORTCUT_PROGRAMS", str(tmp_path / "programs"))
    path = desktop / "Olivia.lnk"
    powershell(["-Command", "$p=[Console]::In.ReadToEnd()|ConvertFrom-Json;"
                "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p.path);"
                "$s.TargetPath=$p.target;$s.Save()"],
               stdin=json.dumps(dict(path=str(path), target=str(root / "START.cmd"))))
    migrate(root, "-RefreshExisting")
    assert shortcut(path)["target"] == str(root / "Olivia.exe")


@pytest.mark.parametrize("skip_new", [False, True])
def test_setup_finalization_creates_or_refreshes_owned_links(tmp_path, skip_new):
    source = (INSTALLER / "Install.ps1").read_text(encoding="utf-8-sig")
    finalize = source[source.rindex("$LASTEXITCODE = 0"):source.rindex("Exit-ManagedInstallLock")]
    (tmp_path / "Create-Shortcut.ps1").write_text(
        "param([string]$InstallRoot, [switch]$RefreshExisting)\n"
        "@{root=$InstallRoot;refresh=[bool]$RefreshExisting}|ConvertTo-Json -Compress\n", encoding="utf-8")
    wrapper = tmp_path / "finalize.ps1"
    wrapper.write_text("$Destination=$PSScriptRoot\n$SkipShortcut=$" + str(skip_new).lower()
                       + "\n" + finalize, encoding="utf-8")
    result = json.loads(powershell(["-File", str(wrapper)]).stdout)
    assert result == {"root": str(tmp_path), "refresh": skip_new}


@pytest.mark.parametrize("kind", ["cmd", "vbs", "native"])
def test_migrates_both_locations_and_removes_old_name(installation, tmp_path, kind):
    root = installation
    for folder in ("desktop", "programs"):
        old = tmp_path / folder / "Olivia 本地版.lnk"
        target = root / ("START.cmd" if kind == "cmd" else "Olivia.exe")
        args = ""
        if kind == "vbs":
            target = Path(os.environ["WINDIR"]) / "System32/wscript.exe"
            args = '//B //Nologo "' + str(root / "START.vbs") + '"'
        shortcut(old, target, args)
    migrate(root, "-RefreshExisting")
    for folder in ("desktop", "programs"):
        assert not (tmp_path / folder / "Olivia 本地版.lnk").exists()
        value = shortcut(tmp_path / folder / "Olivia.lnk")
        assert value["target"] == str(root / "Olivia.exe")
        assert value["arguments"] == ""
        assert value["cwd"] == str(root)
        assert value["icon"] == str(root / "olivia.ico") + ",0"


def test_new_install_and_uninstall_preserve_custom_shortcuts(installation, tmp_path):
    root = installation
    custom = tmp_path / "programs/Olivia.lnk"
    shortcut(custom, root / "unrelated.exe")
    before = custom.read_bytes()
    migrate(root)
    desktop = tmp_path / "desktop/Olivia.lnk"
    assert desktop.exists()
    legacy = tmp_path / "desktop/Olivia 本地版.lnk"
    shortcut(legacy, root / "START.cmd")
    legacy.chmod(0o444)
    renamed = tmp_path / "desktop/My Shortcut.lnk"
    shortcut(renamed, root / "START.cmd")
    migrate(root, "-RemoveExisting")
    assert not desktop.exists() and not legacy.exists()
    assert custom.read_bytes() == before and renamed.exists()


@pytest.mark.parametrize("custom_kind", ["foreign-cmd", "foreign-vbs", "extra-vbs-args", "collision"])
def test_refresh_preserves_user_shortcuts(installation, tmp_path, custom_kind):
    root = installation
    old = tmp_path / "desktop/Olivia 本地版.lnk"
    wscript = Path(os.environ["WINDIR"]) / "System32/wscript.exe"
    if custom_kind == "foreign-cmd":
        shortcut(old, tmp_path / "other/START.cmd")
    elif custom_kind == "foreign-vbs":
        shortcut(old, wscript, '//B //Nologo "' + str(tmp_path / "other/START.vbs") + '"')
    elif custom_kind == "extra-vbs-args":
        shortcut(old, wscript, '//B //Nologo "' + str(root / "START.vbs") + '" /custom')
    else:
        shortcut(old, root / "START.cmd")
        shortcut(old.with_name("Olivia.lnk"), root / "custom.exe")
    before = {p: p.read_bytes() for p in old.parent.glob("*.lnk")}
    migrate(root, "-RefreshExisting")
    assert {p: p.read_bytes() for p in old.parent.glob("*.lnk")} == before


def test_readonly_failure_does_not_block_other_folder(installation, tmp_path):
    old = tmp_path / "desktop/Olivia 本地版.lnk"
    other = tmp_path / "programs/Olivia 本地版.lnk"
    shortcut(old, installation / "START.cmd")
    shortcut(other, installation / "START.cmd")
    before = old.read_bytes()
    old.chmod(0o444)
    try:
        result = migrate(installation, "-RefreshExisting")
        assert 'failed' in result.stdout
        assert old.read_bytes() == before
        assert not old.with_name("Olivia.lnk").exists()
        assert other.with_name("Olivia.lnk").exists() and not other.exists()
    finally:
        old.chmod(0o666)


def test_installed_native_hash_is_checked_before_publish(tmp_path, native):
    backend = tmp_path / "backend"
    target = backend / "installer/olivia_launcher/Olivia.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(native.read_bytes())
    target.with_suffix(".exe.sha256").write_text("0" * 64)
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        install_native_launcher(tmp_path / "install", backend)
    assert not (tmp_path / "install/Olivia.exe").exists()


def test_exit_mapping_and_quoted_arguments_with_compiled_harness(tmp_path):
    # Compile the real Launch/ShouldReport methods with a console test entrypoint.
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    fixture = tmp_path / "Harness.cs"
    fixture.write_text('''using System;
class Harness { static int Main(string[] args) {
if (args.Length == 1) { Console.Write(Program.ShouldReport(unchecked((int)UInt32.Parse(args[0])))); return 0; }
return Program.Launch(args[0], args[1]); }}''')
    harness = tmp_path / "harness.exe"
    subprocess.run([str(compiler), "/nologo", "/main:Harness", "/reference:System.Windows.Forms.dll",
                    "/out:" + str(harness), str(INSTALLER / "olivia_launcher/Program.cs"), str(fixture)], check=True, capture_output=True)
    for code, expected in ((0, "False"), (4294967295, "False"), (2, "True"), (17, "True")):
        assert subprocess.check_output([str(harness), str(code)], text=True) == expected
    root = tmp_path / ("中文 space " * 6 + "end") / "install"
    (root / "launcher").mkdir(parents=True)
    assert subprocess.run([str(harness), str(root), "8899"]).returncode == 2
    runtime = root.parent / "runtime/python-3.12.10-embed-amd64"
    runtime.mkdir(parents=True)
    stub = tmp_path / "Stub.cs"
    stub.write_text('''using System; using System.IO;
class Stub { static int Main(string[] args) {
File.WriteAllLines(Path.Combine(Environment.CurrentDirectory, "argv.txt"), args);
File.WriteAllText(Path.Combine(Environment.CurrentDirectory, "port.txt"), Environment.GetEnvironmentVariable("OLIVIA_PORT"));
return 17; }}''')
    subprocess.run([str(compiler), "/nologo", "/target:winexe", "/out:" + str(runtime / "pythonw.exe"), str(stub)], check=True, capture_output=True)
    assert subprocess.run([str(harness), str(root), "8899"]).returncode == 2
    (root / "launcher/version_launcher.py").touch()
    assert subprocess.run([str(harness), str(root), "8911"]).returncode == 17
    assert (root / "argv.txt").read_text(encoding="utf-8").splitlines() == [str(root / "launcher/version_launcher.py"), "--install-root", str(root), "start", "--port", "8911"]
    assert (root / "port.txt").read_text(encoding="utf-8") == "8911"
