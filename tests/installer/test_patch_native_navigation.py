from __future__ import annotations

from pathlib import Path
import zipfile

from installer.patch_native_navigation import patch_native_navigation


MAIN_MEMBER = "assets/main-31595bd3.js"
HOME_ROUTE = (
    '!z.isNew||$?(await t.replace({name:ve.Home}),' 
    'await h(z.uid.toString(),z.modelGatewayToken||"",!1))'
)
MAILBOX_DISABLED = "N3=!1,Ss=!1,wa=({onComplete"
MAILBOX_ENABLED = "N3=!0,Ss=!1,wa=({onComplete"
OFFLINE_WIDGETS_DISABLED = (
    "e.isOfflineMode&&(l.value.mailWidget!==!1&&(l.value.mailWidget=!1),"
    "l.value.musicWidget!==!1&&(l.value.musicWidget=!1))"
)
OFFLINE_WIDGETS_ENABLED = "l.value.mailWidget=!0,l.value.musicWidget=!0"


def _write_feapp(path: Path, javascript: str) -> bytes:
    path.parent.mkdir(parents=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MAIN_MEMBER, javascript)
        archive.writestr("assets/index.css", "body{display:block}")
    return path.read_bytes()


def _read_main(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read(MAIN_MEMBER).decode("utf-8")


def test_patch_native_navigation_enables_widgets_without_changing_home_route(
    tmp_path: Path,
) -> None:
    client_root = tmp_path / "0.0.9.627"
    feapp = client_root / "resources" / "feapp.dat"
    original = _write_feapp(
        feapp,
        HOME_ROUTE + MAILBOX_DISABLED + OFFLINE_WIDGETS_DISABLED,
    )

    result = patch_native_navigation(client_root, work_root=tmp_path)

    patched = _read_main(feapp)
    backup = feapp.with_name("feapp.dat.native-nav.orig")
    assert result["status"] == "PATCHED"
    assert result["client_version"] == "0.0.9.627"
    assert backup.read_bytes() == original
    assert MAILBOX_ENABLED in patched
    assert MAILBOX_DISABLED not in patched
    assert OFFLINE_WIDGETS_ENABLED in patched
    assert OFFLINE_WIDGETS_DISABLED not in patched
    assert HOME_ROUTE in patched
    assert 'localStorage.setItem("appMode","lite")' not in patched
    assert "await t.replace({name:ve.Collection})" not in patched
