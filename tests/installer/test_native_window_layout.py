import os
from pathlib import Path
from threading import Event

import pytest

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
        self.moves: list[tuple[int, Rect]] = []

    def visible_windows(self) -> tuple[WindowSnapshot, ...]:
        return next(self.observations)

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
    assert api.moves == [(2, Rect(0, 0, 1021, 914))]


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


@pytest.mark.skipif(os.name != "nt", reason="Win32 bindings are Windows-only")
def test_win32_factory_loads_without_changing_a_window() -> None:
    assert create_window_api() is not None
