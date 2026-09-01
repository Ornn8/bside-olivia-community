"""Keep Olivia's native sidebar beside its main window on Windows desktops."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
from threading import Event
import time
from typing import Protocol


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
    NOT_OBSERVED = "not_observed"
    SKIPPED = "skipped"
    UNSUPPORTED = "unsupported"


class WindowApi(Protocol):
    def visible_windows(self) -> tuple[WindowSnapshot, ...]: ...

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
        (w for w in matching if w.rect.width >= 600 and w.rect.height >= 400),
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
    api: WindowApi, pair: tuple[WindowSnapshot, WindowSnapshot]
) -> LayoutStatus:
    status, target = _pair_target(api, pair)
    if status is not LayoutStatus.ADJUSTED or target is None:
        return status
    return (
        LayoutStatus.ADJUSTED
        if api.move_window(pair[0].handle, target)
        else LayoutStatus.SKIPPED
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
    return LayoutStatus.NOT_OBSERVED if pair is None else _adjust_pair(api, pair)


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
            status = _adjust_pair(api, pair)
            if status is not LayoutStatus.ADJUSTED:
                return status
            remaining = max(0.0, deadline - time.monotonic())
            if stop.wait(min(max(0.0, poll_interval), remaining)):
                return LayoutStatus.SKIPPED
            if time.monotonic() >= deadline:
                return LayoutStatus.SKIPPED
            verified = _window_pair(api.visible_windows(), client_executable)
            if stop.is_set():
                return LayoutStatus.SKIPPED
            if verified is None:
                return LayoutStatus.SKIPPED
            return (
                LayoutStatus.ADJUSTED
                if _pair_target(api, verified)[0] is LayoutStatus.ALREADY_VISIBLE
                else LayoutStatus.SKIPPED
            )
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
        flags = 0x0004 | 0x0010 | 0x0200  # no z-order, activation, or owner z-order
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
