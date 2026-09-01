import os
from pathlib import Path
from threading import Event

import pytest

from installer import native_window_layout
from installer import start_local
from installer.native_window_layout import (
    LayoutStatus,
    Rect,
    WindowSnapshot,
    adjust_native_window_layout,
    create_window_api,
    guard_native_window_layout,
    plan_native_window_layout,
)


@pytest.mark.parametrize(
    ("main", "sidebar", "work", "expected"),
    (
        (
            Rect(0, 0, 1100, 914),
            Rect(1103, 64, 1159, 180),
            Rect(0, 0, 1080, 1040),
            Rect(0, 0, 1021, 914),
        ),
        (Rect(-1940, 30, -840, 944), Rect(-837, 94, -781, 210), Rect(-1920, 0, 0, 1040), Rect(-1940, 30, -840, 944)),
        (Rect(100, -20, 1000, 894), Rect(1003, 44, 1059, 160), Rect(0, 0, 1080, 1040), Rect(100, -20, 1000, 894)),
        (Rect(100, 30, 1100, 944), Rect(1103, 94, 1159, 210), Rect(0, 0, 1080, 1040), Rect(100, 30, 1021, 944)),
        (Rect(20, 30, 920, 944), Rect(923, 94, 979, 210), Rect(0, 0, 1080, 1040), Rect(20, 30, 920, 944)),
        (Rect(0, 0, 900, 914), Rect(903, 64, 959, 180), Rect(0, 0, 640, 1040), None),
        (Rect(0, 0, 1000, 900), Rect(800, 50, 856, 166), Rect(0, 0, 1080, 1040), None),
    ),
    ids=("wide", "negative-monitor", "vertical", "fixed-origin", "visible", "narrow", "not-right"),
)
def test_layout_plan_keeps_the_native_pair_in_one_work_area(
    main: Rect,
    sidebar: Rect,
    work: Rect,
    expected: Rect | None,
) -> None:
    assert plan_native_window_layout(
        main=main, sidebar=sidebar, work_area=work
    ) == expected


class FakeApi:
    def __init__(self, *observations: tuple[WindowSnapshot, ...]) -> None:
        self.observations = iter(observations)
        self.current_windows: tuple[WindowSnapshot, ...] = ()
        self.moves: list[tuple[int, Rect]] = []

    def visible_windows(self) -> tuple[WindowSnapshot, ...]:
        self.current_windows = next(self.observations)
        return self.current_windows

    def window_snapshot(self, handle: int) -> WindowSnapshot | None:
        return next(
            (window for window in self.current_windows if window.handle == handle),
            None,
        )

    def monitor_geometry(self, _handle: int) -> tuple[Rect, Rect]:
        return Rect(0, 0, 1080, 1080), Rect(0, 0, 1080, 1040)

    def move_window(self, handle: int, target: Rect) -> bool:
        self.moves.append((handle, target))
        return True


def _pair(client: Path, *, main: Rect = Rect(0, 0, 1100, 914)) -> tuple[WindowSnapshot, ...]:
    return (
        WindowSnapshot(2, main, client, process_id=10),
        WindowSnapshot(
            3,
            Rect(main.right + 3, 64, main.right + 59, 180),
            client,
            process_id=10,
        ),
    )


def test_pairing_never_combines_windows_from_different_processes() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    main = WindowSnapshot(
        2,
        Rect(0, 0, 1100, 914),
        client,
        process_id=10,
    )
    wrong_process_sidebar = WindowSnapshot(
        3,
        Rect(1103, 64, 1159, 180),
        client,
        process_id=11,
    )
    correct_sidebar = WindowSnapshot(
        4,
        Rect(1110, 64, 1166, 180),
        client,
        process_id=10,
    )
    api = FakeApi((main, wrong_process_sidebar, correct_sidebar))

    assert adjust_native_window_layout(client, api=api) is LayoutStatus.ADJUSTED
    assert api.moves == [(2, Rect(0, 0, 1014, 914))]


def test_pairing_rejects_unknown_process_ids() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    main, sidebar = _pair(client)
    api = FakeApi(
        (
            WindowSnapshot(main.handle, main.rect, client, process_id=0),
            WindowSnapshot(sidebar.handle, sidebar.rect, client, process_id=0),
        )
    )

    assert adjust_native_window_layout(client, api=api) is LayoutStatus.NOT_OBSERVED
    assert api.moves == []


def test_adjustment_pairs_before_ranking_and_moves_only_the_matching_main() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    windows = (
        WindowSnapshot(1, Rect(0, 0, 1900, 1000), Path(r"C:\Other\Olivia.exe")),
        WindowSnapshot(4, Rect(0, 0, 1920, 1080), client),
        *_pair(client),
        WindowSnapshot(5, Rect(20, 20, 500, 300), client),
    )
    api = FakeApi(windows)

    assert adjust_native_window_layout(client, api=api) is LayoutStatus.ADJUSTED
    assert api.moves == [(2, Rect(0, 0, 1021, 914))]


def test_adjustment_rejects_a_reused_main_handle_before_the_forward_move() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    pair = _pair(client)

    class ReusedHandleApi(FakeApi):
        def window_snapshot(self, handle: int) -> WindowSnapshot | None:
            current = super().window_snapshot(handle)
            if current is None:
                return None
            return WindowSnapshot(
                current.handle,
                current.rect,
                current.executable,
                process_id=11,
            )

    api = ReusedHandleApi(pair)

    assert adjust_native_window_layout(client, api=api) is LayoutStatus.SKIPPED
    assert api.moves == []


def test_adjustment_rejects_a_reused_main_handle_with_a_different_executable() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    pair = _pair(client)

    class ReusedHandleApi(FakeApi):
        def window_snapshot(self, handle: int) -> WindowSnapshot | None:
            current = super().window_snapshot(handle)
            if current is None:
                return None
            return WindowSnapshot(
                current.handle,
                current.rect,
                Path(r"C:\Other\Olivia.exe"),
                process_id=current.process_id,
            )

    api = ReusedHandleApi(pair)

    assert adjust_native_window_layout(client, api=api) is LayoutStatus.SKIPPED
    assert api.moves == []


def test_adjustment_preserves_main_when_sidebar_is_visible_on_adjacent_monitor() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")

    class AdjacentMonitorApi(FakeApi):
        def monitor_geometry(self, handle: int) -> tuple[Rect, Rect]:
            if handle == 3:
                return Rect(1080, 0, 2160, 1080), Rect(1080, 0, 2160, 1040)
            return super().monitor_geometry(handle)

    api = AdjacentMonitorApi(_pair(client))

    assert adjust_native_window_layout(client, api=api) is LayoutStatus.ALREADY_VISIBLE
    assert api.moves == []


@pytest.mark.parametrize("state", ("minimized", "maximized", "fullscreen"))
def test_adjustment_leaves_special_window_states_untouched(state: str) -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    main = Rect(0, 0, 1080, 1080) if state == "fullscreen" else Rect(0, 0, 1100, 914)
    windows = list(_pair(client, main=main))
    windows[0] = WindowSnapshot(
        2,
        main,
        client,
        process_id=10,
        minimized=state == "minimized",
        maximized=state == "maximized",
    )
    api = FakeApi(tuple(windows))

    assert adjust_native_window_layout(client, api=api) is LayoutStatus.SKIPPED
    assert api.moves == []


def test_guard_waits_for_stability_then_verifies_that_the_sidebar_followed() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    pair = _pair(client)
    adjusted = _pair(client, main=Rect(0, 0, 1021, 914))
    api = FakeApi((), pair, pair, adjusted)

    assert guard_native_window_layout(
        client, api=api, timeout_seconds=1, poll_interval=0
    ) is LayoutStatus.ADJUSTED
    assert api.moves == [(2, Rect(0, 0, 1021, 914))]


def test_guard_never_chases_a_sidebar_that_did_not_follow_the_first_move() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    pair = _pair(client)
    sticky = (
        WindowSnapshot(2, Rect(0, 0, 1021, 914), client, process_id=10),
        pair[1],
    )
    api = FakeApi(pair, pair, sticky)

    assert guard_native_window_layout(
        client, api=api, timeout_seconds=1, poll_interval=0
    ) is LayoutStatus.SKIPPED
    assert api.moves == [
        (2, Rect(0, 0, 1021, 914)),
        (2, Rect(0, 0, 1100, 914)),
    ]


def test_guard_reports_failed_when_the_protected_rollback_fails() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    pair = _pair(client)
    sticky = (
        WindowSnapshot(2, Rect(0, 0, 1021, 914), client, process_id=10),
        pair[1],
    )

    class RollbackFailureApi(FakeApi):
        def move_window(self, handle: int, target: Rect) -> bool:
            self.moves.append((handle, target))
            return len(self.moves) == 1

    api = RollbackFailureApi(pair, pair, sticky)

    assert guard_native_window_layout(
        client, api=api, timeout_seconds=1, poll_interval=0
    ) is LayoutStatus.FAILED
    assert api.moves == [
        (2, Rect(0, 0, 1021, 914)),
        (2, Rect(0, 0, 1100, 914)),
    ]


def test_client_launch_runs_the_native_layout_guard_until_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = tmp_path / "app" / "Olivia.exe"
    local = tmp_path / "profile" / "Local"
    data_root = tmp_path / "data"
    started = Event()
    stopped = Event()
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    events: list[tuple[str, dict[str, object]]] = []

    def guard(
        executable: Path,
        *,
        stop_event: Event,
    ) -> LayoutStatus:
        assert executable == client
        started.set()
        assert stop_event.wait(1)
        stopped.set()
        return LayoutStatus.SKIPPED

    def call(command: list[str], *, cwd: Path, env: dict[str, str]) -> int:
        assert started.wait(1)
        calls.append((command, cwd, env))
        return 0

    monkeypatch.setattr(start_local, "guard_native_window_layout", guard)
    monkeypatch.setattr(start_local.subprocess, "call", call)
    monkeypatch.setattr(
        start_local,
        "_append_launcher_event",
        lambda _root, event, **fields: events.append((event, fields)),
    )

    assert start_local._run_client_with_native_layout(
        client,
        local,
        cwd=client.parent,
        environment={"SYNTHETIC": "1"},
        data_root=data_root,
        attempt=1,
    ) == 0
    assert stopped.is_set()
    assert calls == [
        (
            [str(client), f"--user-data-dir={local / 'cef'}"],
            client.parent,
            {"SYNTHETIC": "1"},
        )
    ]
    assert events == [("client_layout", {"attempt": 1, "status": "skipped"})]


def test_guard_does_not_verify_with_another_client_instance() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    pair = _pair(client)
    other_instance = (
        WindowSnapshot(20, Rect(0, 0, 1021, 914), client, process_id=10),
        WindowSnapshot(30, Rect(1024, 64, 1080, 180), client, process_id=10),
    )
    api = FakeApi(pair, pair, other_instance)

    assert guard_native_window_layout(
        client, api=api, timeout_seconds=1, poll_interval=0
    ) is LayoutStatus.FAILED
    assert api.moves == [(2, Rect(0, 0, 1021, 914))]


def test_guard_verifies_original_pair_when_another_instance_is_larger() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    pair = _pair(client)
    adjusted = _pair(client, main=Rect(0, 0, 1021, 914))
    larger_instance = (
        WindowSnapshot(20, Rect(0, 0, 1050, 914), client, process_id=11),
        WindowSnapshot(30, Rect(1053, 64, 1109, 180), client, process_id=11),
    )
    api = FakeApi(pair, pair, (*adjusted, *larger_instance))

    assert guard_native_window_layout(
        client, api=api, timeout_seconds=1, poll_interval=0
    ) is LayoutStatus.ADJUSTED
    assert api.moves == [(2, Rect(0, 0, 1021, 914))]


def test_guard_does_not_verify_when_the_original_handles_change_process() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    pair = _pair(client)
    reused_handles = (
        WindowSnapshot(2, Rect(0, 0, 1021, 914), client, process_id=11),
        WindowSnapshot(3, Rect(1024, 64, 1080, 180), client, process_id=11),
    )
    api = FakeApi(pair, pair, reused_handles)

    assert guard_native_window_layout(
        client, api=api, timeout_seconds=1, poll_interval=0
    ) is LayoutStatus.FAILED
    assert api.moves == [(2, Rect(0, 0, 1021, 914))]


def test_guard_fails_safe_when_the_target_window_repositions_itself() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    pair = _pair(client)
    self_repositioned = (
        WindowSnapshot(2, Rect(20, 0, 1021, 914), client, process_id=10),
        WindowSnapshot(3, Rect(1024, 64, 1080, 180), client, process_id=10),
    )
    api = FakeApi(pair, pair, self_repositioned)

    assert guard_native_window_layout(
        client, api=api, timeout_seconds=1, poll_interval=0
    ) is LayoutStatus.SKIPPED
    assert api.moves == [
        (2, Rect(0, 0, 1021, 914)),
        (2, Rect(0, 0, 1100, 914)),
    ]


def test_guard_verifies_the_original_sidebar_reached_its_planned_rect() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    pair = _pair(client)
    independently_moved_sidebar = (
        WindowSnapshot(2, Rect(0, 0, 1021, 914), client, process_id=10),
        WindowSnapshot(3, Rect(1024, 100, 1080, 216), client, process_id=10),
    )
    api = FakeApi(pair, pair, independently_moved_sidebar)

    assert guard_native_window_layout(
        client, api=api, timeout_seconds=1, poll_interval=0
    ) is LayoutStatus.SKIPPED
    assert api.moves == [
        (2, Rect(0, 0, 1021, 914)),
        (2, Rect(0, 0, 1100, 914)),
    ]


def test_guard_rechecks_cancellation_before_moving_a_stable_pair() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    stop = Event()
    pair = _pair(client)

    class CancellingApi(FakeApi):
        def visible_windows(self) -> tuple[WindowSnapshot, ...]:
            value = super().visible_windows()
            if value == pair and not stop.is_set() and hasattr(self, "seen_pair"):
                stop.set()
            self.seen_pair = True
            return value

    api = CancellingApi(pair, pair)

    assert guard_native_window_layout(
        client,
        api=api,
        stop_event=stop,
        timeout_seconds=1,
        poll_interval=0,
    ) is LayoutStatus.SKIPPED
    assert api.moves == []


def test_guard_rechecks_cancellation_after_geometry_before_moving() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    stop = Event()
    pair = _pair(client)

    class GeometryCancellingApi(FakeApi):
        def monitor_geometry(self, handle: int) -> tuple[Rect, Rect]:
            geometry = super().monitor_geometry(handle)
            if handle == pair[1].handle:
                stop.set()
            return geometry

    api = GeometryCancellingApi(pair, pair)

    assert guard_native_window_layout(
        client,
        api=api,
        stop_event=stop,
        timeout_seconds=1,
        poll_interval=0,
    ) is LayoutStatus.SKIPPED
    assert api.moves == []


def test_guard_rolls_back_when_cancelled_after_the_forward_move() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    pair = _pair(client)

    class StopAfterForward:
        waits = 0
        stopped = False

        def is_set(self) -> bool:
            return self.stopped

        def wait(self, _timeout: float) -> bool:
            self.waits += 1
            if self.waits == 2:
                self.stopped = True
            return self.stopped

    api = FakeApi(pair, pair)

    assert guard_native_window_layout(
        client,
        api=api,
        stop_event=StopAfterForward(),  # type: ignore[arg-type]
        timeout_seconds=1,
        poll_interval=0,
    ) is LayoutStatus.SKIPPED
    assert api.moves == [
        (2, Rect(0, 0, 1021, 914)),
        (2, Rect(0, 0, 1100, 914)),
    ]


def test_guard_rechecks_deadline_after_geometry_before_moving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    pair = _pair(client)
    now = [0.0]
    monkeypatch.setattr(native_window_layout.time, "monotonic", lambda: now[0])

    class SlowGeometryApi(FakeApi):
        def monitor_geometry(self, handle: int) -> tuple[Rect, Rect]:
            geometry = super().monitor_geometry(handle)
            if handle == pair[1].handle:
                now[0] = 2.0
            return geometry

    api = SlowGeometryApi(pair, pair)

    assert guard_native_window_layout(
        client,
        api=api,
        timeout_seconds=1,
        poll_interval=0,
    ) is LayoutStatus.SKIPPED
    assert api.moves == []


def test_guard_does_not_report_adjusted_after_verification_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    pair = _pair(client)
    adjusted = _pair(client, main=Rect(0, 0, 1021, 914))
    now = [0.0]
    monkeypatch.setattr(native_window_layout.time, "monotonic", lambda: now[0])

    class SlowVerificationApi(FakeApi):
        calls = 0

        def visible_windows(self) -> tuple[WindowSnapshot, ...]:
            value = super().visible_windows()
            self.calls += 1
            if self.calls == 3:
                now[0] = 2.0
            return value

    api = SlowVerificationApi(pair, pair, adjusted)

    assert guard_native_window_layout(
        client,
        api=api,
        timeout_seconds=1,
        poll_interval=0,
    ) is LayoutStatus.SKIPPED
    assert api.moves == [
        (2, Rect(0, 0, 1021, 914)),
        (2, Rect(0, 0, 1100, 914)),
    ]


def test_guard_rechecks_deadline_after_verification_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    pair = _pair(client)
    adjusted = _pair(client, main=Rect(0, 0, 1021, 914))
    now = [0.0]
    monkeypatch.setattr(native_window_layout.time, "monotonic", lambda: now[0])

    class SlowVerificationGeometryApi(FakeApi):
        geometry_calls = 0

        def monitor_geometry(self, handle: int) -> tuple[Rect, Rect]:
            geometry = super().monitor_geometry(handle)
            self.geometry_calls += 1
            if self.geometry_calls == 4:
                now[0] = 2.0
            return geometry

    api = SlowVerificationGeometryApi(pair, pair, adjusted)

    assert guard_native_window_layout(
        client,
        api=api,
        timeout_seconds=1,
        poll_interval=0,
    ) is LayoutStatus.SKIPPED
    assert api.moves == [
        (2, Rect(0, 0, 1021, 914)),
        (2, Rect(0, 0, 1100, 914)),
    ]


def test_guard_rolls_back_when_verification_geometry_disappears() -> None:
    client = Path(r"C:\Olivia\Olivia.exe")
    pair = _pair(client)
    adjusted = _pair(client, main=Rect(0, 0, 1021, 914))

    class MissingVerificationGeometryApi(FakeApi):
        geometry_calls = 0

        def monitor_geometry(self, handle: int) -> tuple[Rect, Rect] | None:
            self.geometry_calls += 1
            if self.geometry_calls == 3:
                return None
            return super().monitor_geometry(handle)

    api = MissingVerificationGeometryApi(pair, pair, adjusted)

    assert guard_native_window_layout(
        client, api=api, timeout_seconds=1, poll_interval=0
    ) is LayoutStatus.SKIPPED
    assert api.moves == [
        (2, Rect(0, 0, 1021, 914)),
        (2, Rect(0, 0, 1100, 914)),
    ]


@pytest.mark.skipif(os.name != "nt", reason="Win32 bindings are Windows-only")
def test_win32_factory_loads_without_changing_a_window() -> None:
    assert create_window_api() is not None


@pytest.mark.skipif(os.name != "nt", reason="Win32 bindings are Windows-only")
def test_win32_move_requests_async_cross_queue_dispatch() -> None:
    api = create_window_api()
    assert api is not None

    class RecordingUser32:
        flags = 0

        def SetWindowPos(self, *_arguments) -> int:
            self.flags = int(_arguments[-1].value)
            return 1

    user32 = RecordingUser32()
    api.user32 = user32  # type: ignore[attr-defined]

    assert api.move_window(2, Rect(10, 20, 810, 620)) is True
    assert user32.flags & 0x4000
