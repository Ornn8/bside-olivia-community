"""Keep Olivia's native sidebar beside its main window on Windows desktops."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
from threading import Event
import time
from typing import Callable, Protocol


@dataclass(frozen=True, slots=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True, slots=True)
class WindowSnapshot:
    handle: int
    rect: Rect
    executable: Path
    process_id: int = 0
    minimized: bool = False
    maximized: bool = False


class LayoutStatus(str, Enum):
    ADJUSTED = "adjusted"
    ALREADY_VISIBLE = "already_visible"
    FAILED = "failed"
    NOT_OBSERVED = "not_observed"
    SKIPPED = "skipped"
    UNSUPPORTED = "unsupported"


class WindowApi(Protocol):
    def visible_windows(self) -> tuple[WindowSnapshot, ...]: ...

    def window_snapshot(self, handle: int) -> WindowSnapshot | None: ...

    def monitor_geometry(self, handle: int) -> tuple[Rect, Rect] | None: ...

    def move_window(self, handle: int, target: Rect) -> bool: ...


def plan_native_window_layout(
    *,
    main: Rect,
    sidebar: Rect,
    work_area: Rect,
    minimum_main_width: int = 640,
) -> Rect | None:
    """Return the main rectangle that keeps its right sidebar in the work area."""

    gap = sidebar.left - main.right
    if (
        not 0 <= gap <= 32
        or sidebar.width <= 0
        or sidebar.height <= 0
        or main.height <= 0
    ):
        return None
    if (
        sidebar.left >= work_area.left
        and sidebar.top >= work_area.top
        and sidebar.right <= work_area.right
        and sidebar.bottom <= work_area.bottom
    ):
        return main
    if (
        sidebar.left < work_area.left
        or sidebar.top < work_area.top
        or sidebar.bottom > work_area.bottom
    ):
        return None
    target_right = work_area.right - gap - sidebar.width
    target_width = target_right - main.left
    if target_width < minimum_main_width or target_width >= main.width:
        return None
    return Rect(
        main.left,
        main.top,
        target_right,
        main.bottom,
    )


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _window_pair(
    windows: tuple[WindowSnapshot, ...], executable: Path
) -> tuple[WindowSnapshot, WindowSnapshot] | None:
    expected = _normalized_path(executable)
    matching = [w for w in windows if _normalized_path(w.executable) == expected]
    mains = sorted(
        (
            w
            for w in matching
            if w.process_id > 0 and w.rect.width >= 600 and w.rect.height >= 400
        ),
        key=lambda w: w.rect.width * w.rect.height,
        reverse=True,
    )
    for main in mains:
        sidebars = [
            w
            for w in matching
            if w.handle != main.handle
            and w.process_id == main.process_id
            and 40 <= w.rect.width <= 96
            and 72 <= w.rect.height <= 240
            and 0 <= w.rect.left - main.rect.right <= 32
            and w.rect.top < main.rect.bottom
            and w.rect.bottom > main.rect.top
        ]
        if sidebars:
            return main, min(
                sidebars,
                key=lambda w: (
                    w.rect.left - main.rect.right,
                    abs(
                        w.rect.top
                        + w.rect.bottom
                        - main.rect.top
                        - main.rect.bottom
                    ),
                ),
            )
    return None


def _pair_target(
    api: WindowApi,
    pair: tuple[WindowSnapshot, WindowSnapshot],
) -> tuple[LayoutStatus, Rect | None]:
    main, sidebar = pair
    geometry = api.monitor_geometry(main.handle)
    if geometry is None:
        return LayoutStatus.SKIPPED, None
    monitor, work_area = geometry
    if main.minimized or main.maximized or (
        main.rect.left <= monitor.left
        and main.rect.top <= monitor.top
        and main.rect.right >= monitor.right
        and main.rect.bottom >= monitor.bottom
    ):
        return LayoutStatus.SKIPPED, None
    sidebar_geometry = api.monitor_geometry(sidebar.handle)
    if sidebar_geometry is not None:
        _, sidebar_work_area = sidebar_geometry
        if (
            sidebar.rect.left >= sidebar_work_area.left
            and sidebar.rect.top >= sidebar_work_area.top
            and sidebar.rect.right <= sidebar_work_area.right
            and sidebar.rect.bottom <= sidebar_work_area.bottom
        ):
            return LayoutStatus.ALREADY_VISIBLE, main.rect
    target = plan_native_window_layout(
        main=main.rect,
        sidebar=sidebar.rect,
        work_area=work_area,
    )
    if target is None:
        return LayoutStatus.SKIPPED, None
    if target == main.rect:
        return LayoutStatus.ALREADY_VISIBLE, target
    return LayoutStatus.ADJUSTED, target


def _adjust_pair(
    api: WindowApi,
    pair: tuple[WindowSnapshot, WindowSnapshot],
    *,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[LayoutStatus, Rect | None]:
    status, target = _pair_target(api, pair)
    if status is not LayoutStatus.ADJUSTED or target is None:
        return status, target
    if cancelled is not None and cancelled():
        return LayoutStatus.SKIPPED, target
    if not _same_main_window(api, pair[0]):
        return LayoutStatus.SKIPPED, target
    moved = api.move_window(pair[0].handle, target)
    return (LayoutStatus.ADJUSTED if moved else LayoutStatus.SKIPPED), target


def _same_main_window(api: WindowApi, expected: WindowSnapshot) -> bool:
    current = api.window_snapshot(expected.handle)
    return bool(
        current is not None
        and expected.process_id > 0
        and current.process_id == expected.process_id
        and _normalized_path(current.executable)
        == _normalized_path(expected.executable)
        and current.rect.width >= 600
        and current.rect.height >= 400
        and not current.minimized
        and not current.maximized
    )


def _rollback_main_window(
    api: WindowApi, original: WindowSnapshot
) -> LayoutStatus:
    if not _same_main_window(api, original):
        return LayoutStatus.FAILED
    return (
        LayoutStatus.SKIPPED
        if api.move_window(original.handle, original.rect)
        else LayoutStatus.FAILED
    )


def create_window_api() -> WindowApi | None:
    """Create the Win32 adapter without changing DPI awareness or focus."""

    if os.name != "nt":
        return None
    try:
        return _CtypesWindowApi()
    except (AttributeError, OSError):
        return None


def adjust_native_window_layout(
    client_executable: Path, *, api: WindowApi | None = None
) -> LayoutStatus:
    """Adjust one observed main/sidebar pair without activating it."""

    api = api or create_window_api()
    if api is None:
        return LayoutStatus.UNSUPPORTED
    pair = _window_pair(api.visible_windows(), client_executable)
    return (
        LayoutStatus.NOT_OBSERVED
        if pair is None
        else _adjust_pair(api, pair)[0]
    )


def guard_native_window_layout(
    client_executable: Path,
    *,
    api: WindowApi | None = None,
    stop_event: Event | None = None,
    timeout_seconds: float = 15.0,
    poll_interval: float = 0.1,
) -> LayoutStatus:
    """Wait for two stable observations, adjust once, and verify once."""

    api = api or create_window_api()
    if api is None:
        return LayoutStatus.UNSUPPORTED
    stop = stop_event or Event()
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    previous = None
    stable = 0
    while not stop.is_set():
        pair = _window_pair(api.visible_windows(), client_executable)
        if stop.is_set():
            return LayoutStatus.SKIPPED
        if pair is None:
            previous, stable = None, 0
        elif pair == previous:
            stable += 1
        else:
            previous, stable = pair, 1
        if pair is not None and stable >= 2:
            if stop.is_set() or time.monotonic() >= deadline:
                return LayoutStatus.SKIPPED
            status, target = _adjust_pair(
                api,
                pair,
                cancelled=lambda: stop.is_set() or time.monotonic() >= deadline,
            )
            if status is not LayoutStatus.ADJUSTED or target is None:
                return status
            remaining = max(0.0, deadline - time.monotonic())
            if stop.wait(min(max(0.0, poll_interval), remaining)):
                return _rollback_main_window(api, pair[0])
            if time.monotonic() >= deadline:
                return _rollback_main_window(api, pair[0])
            observed = api.visible_windows()
            if stop.is_set() or time.monotonic() >= deadline:
                return _rollback_main_window(api, pair[0])
            verified_main = next(
                (
                    window
                    for window in observed
                    if window.handle == pair[0].handle
                    and window.process_id == pair[0].process_id
                ),
                None,
            )
            verified_sidebar = next(
                (
                    window
                    for window in observed
                    if window.handle == pair[1].handle
                    and window.process_id == pair[1].process_id
                ),
                None,
            )
            if verified_main is None or verified_sidebar is None:
                return _rollback_main_window(api, pair[0])
            verified = verified_main, verified_sidebar
            sidebar_dx = target.right - pair[0].rect.right
            sidebar_dy = target.top - pair[0].rect.top
            expected_sidebar = Rect(
                pair[1].rect.left + sidebar_dx,
                pair[1].rect.top + sidebar_dy,
                pair[1].rect.right + sidebar_dx,
                pair[1].rect.bottom + sidebar_dy,
            )
            if (
                verified[0].rect != target
                or verified[1].rect != expected_sidebar
            ):
                return _rollback_main_window(api, pair[0])
            verified_status = _pair_target(api, verified)[0]
            if stop.is_set() or time.monotonic() >= deadline:
                return _rollback_main_window(api, pair[0])
            if verified_status is LayoutStatus.ALREADY_VISIBLE:
                return LayoutStatus.ADJUSTED
            return _rollback_main_window(api, pair[0])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return LayoutStatus.NOT_OBSERVED
        if stop.wait(min(max(0.0, poll_interval), remaining)):
            return LayoutStatus.SKIPPED
    return LayoutStatus.SKIPPED


class _CtypesWindowApi:
    """Small user32/kernel32 boundary; no DPI, focus, or style calls."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        self.c, self.w = ctypes, wintypes
        self.monitor_info = MonitorInfo
        self.callback = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.MonitorFromWindow.restype = wintypes.HANDLE
        self.kernel32.OpenProcess.restype = wintypes.HANDLE

    @staticmethod
    def _rect(value) -> Rect:
        return Rect(value.left, value.top, value.right, value.bottom)

    def _handles(self) -> tuple[int, ...]:
        handles: list[int] = []

        @self.callback
        def collect(handle, _parameter) -> bool:
            handles.append(int(handle))
            return True

        return (
            tuple(handles)
            if self.user32.EnumWindows(collect, self.w.LPARAM(0))
            else ()
        )

    def _executable(self, process_id: int) -> Path | None:
        process = self.kernel32.OpenProcess(
            self.w.DWORD(0x1000), self.w.BOOL(False), self.w.DWORD(process_id)
        )
        if not process:
            return None
        try:
            buffer = self.c.create_unicode_buffer(32768)
            length = self.w.DWORD(len(buffer))
            if not self.kernel32.QueryFullProcessImageNameW(
                self.w.HANDLE(process), self.w.DWORD(0), buffer, self.c.byref(length)
            ):
                return None
            return Path(buffer.value) if buffer.value else None
        finally:
            self.kernel32.CloseHandle(self.w.HANDLE(process))

    def visible_windows(self) -> tuple[WindowSnapshot, ...]:
        windows: list[WindowSnapshot] = []
        executables: dict[int, Path | None] = {}
        for handle in self._handles():
            hwnd = self.w.HWND(handle)
            if not self.user32.IsWindowVisible(hwnd):
                continue
            rect, process_id = self.w.RECT(), self.w.DWORD()
            if not self.user32.GetWindowRect(hwnd, self.c.byref(rect)):
                continue
            self.user32.GetWindowThreadProcessId(hwnd, self.c.byref(process_id))
            if not process_id.value or rect.right <= rect.left or rect.bottom <= rect.top:
                continue
            if process_id.value not in executables:
                executables[process_id.value] = self._executable(process_id.value)
            executable = executables[process_id.value]
            if executable is not None:
                windows.append(
                    WindowSnapshot(
                        handle,
                        self._rect(rect),
                        executable,
                        process_id=int(process_id.value),
                        minimized=bool(self.user32.IsIconic(hwnd)),
                        maximized=bool(self.user32.IsZoomed(hwnd)),
                    )
                )
        return tuple(windows)

    def window_snapshot(self, handle: int) -> WindowSnapshot | None:
        hwnd = self.w.HWND(handle)
        ancestor = self.user32.GetAncestor(hwnd, self.w.UINT(2))
        ancestor_handle = int(getattr(ancestor, "value", ancestor) or 0)
        if (
            not self.user32.IsWindow(hwnd)
            or not self.user32.IsWindowVisible(hwnd)
            or ancestor_handle != handle
        ):
            return None
        rect, process_id = self.w.RECT(), self.w.DWORD()
        if not self.user32.GetWindowRect(hwnd, self.c.byref(rect)):
            return None
        self.user32.GetWindowThreadProcessId(hwnd, self.c.byref(process_id))
        executable = self._executable(int(process_id.value)) if process_id.value else None
        if (
            executable is None
            or rect.right <= rect.left
            or rect.bottom <= rect.top
        ):
            return None
        return WindowSnapshot(
            handle,
            self._rect(rect),
            executable,
            process_id=int(process_id.value),
            minimized=bool(self.user32.IsIconic(hwnd)),
            maximized=bool(self.user32.IsZoomed(hwnd)),
        )

    def monitor_geometry(self, handle: int) -> tuple[Rect, Rect] | None:
        monitor = self.user32.MonitorFromWindow(self.w.HWND(handle), self.w.DWORD(2))
        info = self.monitor_info()
        info.cbSize = self.c.sizeof(info)
        if not monitor or not self.user32.GetMonitorInfoW(
            self.w.HANDLE(monitor), self.c.byref(info)
        ):
            return None
        return self._rect(info.rcMonitor), self._rect(info.rcWork)

    def move_window(self, handle: int, target: Rect) -> bool:
        # Keep z-order/focus unchanged and never block on another input queue.
        flags = 0x0004 | 0x0010 | 0x0200 | 0x4000
        return bool(
            self.user32.SetWindowPos(
                self.w.HWND(handle),
                None,
                target.left,
                target.top,
                target.width,
                target.height,
                self.w.UINT(flags),
            )
        )


__all__ = [
    "LayoutStatus",
    "Rect",
    "WindowApi",
    "WindowSnapshot",
    "adjust_native_window_layout",
    "create_window_api",
    "guard_native_window_layout",
    "plan_native_window_layout",
]
