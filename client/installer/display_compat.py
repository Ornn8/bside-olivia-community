"""Let the client window paint on the graphics adapter Windows picked for it.

The client shows its page from two programs: ``app/<version>/Olivia.exe`` puts the frames on the
screen and ``app/<version>/CefView/Olivia.exe`` draws them; the frames pass between the two as
shared textures. Windows keeps a graphics choice per program (Settings > System > Display >
Graphics, saved under HKCU ``UserGpuPreferences``). When a user sets only the main program, for
example to "Power saving" on a laptop with two adapters, the two programs land on different
adapters and the window stays black.

The compat action available today gives the drawing program the same choice as the main program,
unless the user chose something for the drawing program. ``COMPAT_ACTIONS`` is ordered; a later
action (a client command line switch, say) is added there and is picked by the same fallback.

Mode, saved in ``config/display-compat.json``:

* ``auto`` (default) aligns when two or more display adapters are present, or when an earlier start
  of this install needed it (the remembered fallback);
* ``on`` aligns whenever the main program has a choice;
* ``off`` never writes and removes what this module wrote.

``config/display-compat-state.json`` records what was written, so it can be updated or removed, and
the remembered fallback. Only the drawing program's value is ever written or removed, only in HKCU,
and never when the user made that value.

``ClientDisplayWatch`` follows one client start: when the window exists and the page has reported in
but nothing is painted for a while, it asks the launcher for one compat restart, or only logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import ntpath
import os
from pathlib import Path
from threading import Event, Thread
import time
from typing import Callable, Iterable, Mapping, Protocol

MODES = ("auto", "on", "off")
DEFAULT_MODE = "auto"
CONFIG_NAME = "display-compat.json"
STATE_NAME = "display-compat-state.json"
PAGE_READY_NAME = "client-page-ready.txt"
SCHEMA_VERSION = 1
GOOD_STARTS_BEFORE_RECHECK = 25
ALIGN_ADAPTERS = "align_adapters"
GPU_PREFERENCES_KEY = r"Software\Microsoft\DirectX\UserGpuPreferences"
DISPLAY_CLASS_KEY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
# 0 lets Windows decide, 1 is power saving, 2 is high performance.
CHOSEN_PREFERENCES = frozenset({1, 2})
PAINTED_COLOURS = 16


# --- saved mode -------------------------------------------------------------------------------------------------

def normalize_mode(value: object) -> str | None:
    if isinstance(value, str) and value.strip().lower() in MODES:
        return value.strip().lower()
    return None


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION else {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def read_mode(config_root: Path) -> str:
    """The saved mode; anything missing or unreadable is Auto."""

    return normalize_mode(_read_json(Path(config_root) / CONFIG_NAME).get("mode")) or DEFAULT_MODE


def save_mode(config_root: Path, value: object) -> str:
    mode = normalize_mode(value)
    if mode is None:
        raise ValueError("DISPLAY_COMPAT_MODE_INVALID")
    _write_json(Path(config_root) / CONFIG_NAME, {"schema_version": SCHEMA_VERSION, "mode": mode})
    return mode


def _read_state(config_root: Path) -> dict:
    return _read_json(Path(config_root) / STATE_NAME)


def _save_state(config_root: Path, state: dict) -> None:
    path = Path(config_root) / STATE_NAME
    kept = {key: state[key] for key in ("written", "remembered") if state.get(key)}
    if not kept:
        path.unlink(missing_ok=True)
        return
    _write_json(path, {"schema_version": SCHEMA_VERSION, **kept})


# --- Windows boundary -------------------------------------------------------------------------------------------

class GpuPreferenceStore(Protocol):
    def values(self) -> dict[str, str]: ...

    def write(self, name: str, data: str) -> None: ...

    def delete(self, name: str) -> None: ...


class WindowsGpuPreferenceStore:
    """HKCU per-program graphics choices. Reads every value, writes or removes only the name it is given."""

    def values(self) -> dict[str, str]:
        import winreg
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, GPU_PREFERENCES_KEY, 0, winreg.KEY_READ)
        except OSError:
            return {}
        found: dict[str, str] = {}
        with key:
            index = 0
            while True:
                try:
                    name, data, kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if kind == winreg.REG_SZ and isinstance(data, str):
                    found[name] = data
        return found

    def write(self, name: str, data: str) -> None:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, GPU_PREFERENCES_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, data)

    def delete(self, name: str) -> None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, GPU_PREFERENCES_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def read_display_device_ids() -> list[str] | None:
    """``MatchingDeviceId`` of every display adapter driver instance (``Class\\{display}\\NNNN``)."""

    import winreg
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, DISPLAY_CLASS_KEY, 0, winreg.KEY_READ)
    except OSError:
        return None
    found: list[str] = []
    with root:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            if len(name) != 4 or not name.isdigit():
                continue
            try:
                with winreg.OpenKey(root, name, 0, winreg.KEY_READ) as instance:
                    value, _kind = winreg.QueryValueEx(instance, "MatchingDeviceId")
            except OSError:
                continue
            if isinstance(value, str):
                found.append(value)
    return found


def count_display_adapters(device_ids: Iterable[object] | None) -> int | None:
    """PCI display adapters; ``None`` when the list could not be read."""

    if device_ids is None:
        return None
    return sum(1 for value in device_ids if isinstance(value, str) and value.strip().lower().startswith("pci\\"))


# --- registry values --------------------------------------------------------------------------------------------

def normalize_exe_path(path: object) -> str:
    text = str(path or "").strip().strip('"').replace("/", "\\")
    return ntpath.normpath(text).casefold() if text else ""


def parse_gpu_preference(data: object) -> int | None:
    """``N`` from ``GpuPreference=N;`` among other ``key=value;`` pairs; only 0, 1 and 2 count."""

    if not isinstance(data, str):
        return None
    for part in data.split(";"):
        key, separator, value = part.partition("=")
        if separator and key.strip().casefold() == "gpupreference":
            value = value.strip()
            return int(value) if value in {"0", "1", "2"} else None
    return None


def find_entry(values: Mapping[str, str], executable: object) -> tuple[str, str] | None:
    wanted = normalize_exe_path(executable)
    if not wanted:
        return None
    for name, data in values.items():
        if normalize_exe_path(name) == wanted:
            return name, data
    return None


def helper_executable(client: Path) -> Path:
    """The program that draws the page; Windows treats it as a program of its own."""

    return Path(client).parent / "CefView" / "Olivia.exe"


# --- decision ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DisplayContext:
    mode: str
    client: str
    main_preference: int | None
    helper_name: str
    helper_data: str | None
    helper_ours: bool
    adapters: int | None
    remembered: tuple[str, ...] = ()

    @property
    def helper_preference(self) -> int | None:
        return parse_gpu_preference(self.helper_data)

    @property
    def helper_owner(self) -> str:
        return "none" if self.helper_data is None else "launcher" if self.helper_ours else "user"


class CompatAction:
    """One way to help the window paint; ``switches`` are extra client command line switches."""

    name = ""
    switches: tuple[str, ...] = ()

    def available(self, context: DisplayContext) -> bool:
        raise NotImplementedError

    def automatic(self, context: DisplayContext) -> bool:
        """Whether Auto applies it before any start went wrong."""
        raise NotImplementedError


class AlignAdapters(CompatAction):
    """Give the drawing program the main program's graphics choice."""

    name = ALIGN_ADAPTERS

    def available(self, context: DisplayContext) -> bool:
        return context.main_preference in CHOSEN_PREFERENCES and (context.helper_data is None or context.helper_ours)

    def automatic(self, context: DisplayContext) -> bool:
        return (context.adapters or 0) >= 2


COMPAT_ACTIONS: tuple[CompatAction, ...] = (AlignAdapters(),)


def _action(name: str) -> CompatAction | None:
    return next((action for action in COMPAT_ACTIONS if action.name == name), None)


def wanted_actions(context: DisplayContext) -> tuple[str, ...]:
    """The compat actions this start applies."""

    if context.mode == "off":
        return ()
    return tuple(
        action.name for action in COMPAT_ACTIONS
        if action.available(context)
        and (context.mode == "on" or action.automatic(context) or action.name in context.remembered)
    )


@dataclass
class DisplaySystem:
    """The parts of Windows used here; tests replace them so no test reads or writes the real registry."""

    store: Callable[[], GpuPreferenceStore] = WindowsGpuPreferenceStore
    device_ids: Callable[[], list[str] | None] = read_display_device_ids
    probe: Callable[[int], "WindowProbe"] | None = None
    watch: bool = os.name == "nt"


SYSTEM = DisplaySystem()


@dataclass(frozen=True)
class DisplayPlan:
    context: DisplayContext
    applied: tuple[str, ...]
    switches: tuple[str, ...]
    fields: dict = field(default_factory=dict)


def _context(client: Path, mode: str, values: Mapping[str, str], adapters: int | None, state: dict) -> DisplayContext:
    main = find_entry(values, client)
    if main is not None:
        helper_name = ntpath.join(ntpath.dirname(main[0].replace("/", "\\")), "CefView", "Olivia.exe")
    else:
        helper_name = str(helper_executable(client)).replace("/", "\\")
    helper = find_entry(values, helper_name)
    if helper is not None:
        helper_name = helper[0]
    written = state.get("written") if isinstance(state.get("written"), dict) else {}
    ours = bool(
        helper is not None and written
        and normalize_exe_path(written.get("path")) == normalize_exe_path(helper_name)
        and written.get("value") == helper[1]
    )
    return DisplayContext(
        mode=mode, client=normalize_exe_path(client),
        main_preference=parse_gpu_preference(main[1]) if main else None,
        helper_name=helper_name, helper_data=helper[1] if helper else None, helper_ours=ours,
        adapters=adapters,
    )


def _remembered(state: dict, context: DisplayContext) -> tuple[str, ...]:
    """The remembered fallback, dropped when Windows' choice changed, after enough good starts, or in Off."""

    memory = state.get("remembered")
    if not isinstance(memory, dict):
        state.pop("remembered", None)
        return ()
    good = memory.get("good_starts")
    actions = memory.get("actions")
    if (context.mode == "off" or memory.get("preference") != context.main_preference
            or memory.get("client") != context.client
            or type(good) is not int or good >= GOOD_STARTS_BEFORE_RECHECK
            or not isinstance(actions, list)):
        state.pop("remembered", None)
        return ()
    return tuple(name for name in actions if isinstance(name, str) and _action(name) is not None)


def prepare_display(client: Path, config_root: Path, *, system: DisplaySystem | None = None,
                    now: float | None = None) -> DisplayPlan:
    """Bring the drawing program's graphics choice in line with the mode before a client start."""

    system = system or SYSTEM
    config_root = Path(config_root)
    stamp = time.time() if now is None else now
    state = _read_state(config_root)
    store = system.store()
    values = store.values()
    adapters = count_display_adapters(system.device_ids())
    context = _context(client, read_mode(config_root), values, adapters, state)
    context = replace(context, remembered=_remembered(state, context))
    before = context.helper_preference
    wanted = wanted_actions(context)
    action = "none"
    written = state.get("written") if isinstance(state.get("written"), dict) else None
    target = f"GpuPreference={context.main_preference};" if ALIGN_ADAPTERS in wanted else None
    if written and not (target and normalize_exe_path(written.get("path")) == normalize_exe_path(context.helper_name)):
        state.pop("written", None)
        entry = find_entry(values, written.get("path"))
        if entry is not None and entry[1] == written.get("value"):
            store.delete(entry[0])
            action = "align_removed"
    if target:
        if context.helper_data != target:
            store.write(context.helper_name, target)
            action = "align_written"
        else:
            action = "align_kept"
        state["written"] = {"path": context.helper_name, "value": target, "mirrors": context.main_preference,
                            "client": context.client, "at": round(stamp, 3)}
    _save_state(config_root, state)
    after = parse_gpu_preference(target) if target else (None if action == "align_removed" else before)
    switches = tuple(switch for name in wanted if (item := _action(name)) for switch in item.switches)
    fields = {
        "mode": context.mode, "main_preference": context.main_preference,
        "helper_preference_before": before, "helper_preference_after": after,
        "helper_owner": context.helper_owner, "adapters": context.adapters, "action": action,
        "remembered": bool(context.remembered),
    }
    return DisplayPlan(context=context, applied=wanted, switches=switches, fields=fields)


def fallback_action(plan: DisplayPlan) -> str | None:
    """The next compat action one restart can try; Auto only, and never one this start already has."""

    if plan.context.mode != "auto":
        return None
    return next((action.name for action in COMPAT_ACTIONS
                 if action.name not in plan.applied and action.available(plan.context)), None)


def remember_fallback(client: Path, config_root: Path, name: str, *, system: DisplaySystem | None = None,
                      now: float | None = None) -> DisplayPlan:
    """Keep the action for the next starts of this install, then apply it."""

    system = system or SYSTEM
    stamp = time.time() if now is None else now
    state = _read_state(config_root)
    values = system.store().values()
    context = _context(client, read_mode(config_root), values, None, state)
    actions = list(_remembered(state, context))
    if name not in actions:
        actions.append(name)
    state["remembered"] = {"actions": actions, "preference": context.main_preference, "client": context.client,
                           "good_starts": 0, "outcome": "pending", "at": round(stamp, 3)}
    _save_state(config_root, state)
    return prepare_display(client, config_root, system=system, now=stamp)


def record_outcome(config_root: Path, painted: bool, *, applied: Iterable[str]) -> None:
    """Count a good start for the remembered fallback; after enough of them the next start checks again."""

    state = _read_state(config_root)
    memory = state.get("remembered")
    if not isinstance(memory, dict) or not isinstance(memory.get("actions"), list):
        return
    if not set(memory["actions"]) <= set(applied):
        return
    if memory.get("outcome") == "pending":
        memory["outcome"] = "painted" if painted else "unpainted"
    if painted:
        memory["good_starts"] = int(memory.get("good_starts") or 0) + 1
        if memory["good_starts"] >= GOOD_STARTS_BEFORE_RECHECK:
            state.pop("remembered", None)
    _save_state(config_root, state)


# --- page ready marker ------------------------------------------------------------------------------------------

def mark_page_ready(data_root: str | Path | None = None, *, now: float | None = None) -> bool:
    """Written when the page first reports to the backend; the launcher compares it with the client start."""

    root = data_root or os.environ.get("OLIVIA_LOCAL_DATA_ROOT")
    if not root:
        return False
    try:
        path = Path(root) / "logs" / PAGE_READY_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{time.time() if now is None else now:.3f}\n", encoding="ascii")
        return True
    except OSError:
        return False


def page_ready_since(data_root: Path, started: float) -> bool:
    try:
        stamp = float((Path(data_root) / "logs" / PAGE_READY_NAME).read_text(encoding="ascii").strip())
    except (OSError, UnicodeError, ValueError):
        return False
    return stamp >= started


# --- window probe -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowProbe:
    window: bool = False
    surface: bool = False
    colours: int = 0

    @property
    def painted(self) -> bool:
        return self.colours >= PAINTED_COLOURS


def count_colours(pixels: bytes | bytearray | memoryview, width: int, height: int, *, stride: int | None = None) -> int:
    """Distinct colours on the splash's sampling grid of a 32-bit BGRA buffer (alpha ignored)."""

    stride = width * 4 if stride is None else stride
    colours = set()
    for y in range(80, height - 20, 17):
        row = y * stride
        for x in range(30, width - 20, 17):
            index = row + x * 4
            colours.add(pixels[index] | pixels[index + 1] << 8 | pixels[index + 2] << 16)
    return len(colours)


def _page_surface_class(name: str) -> bool:
    return name.startswith("Chrome_RenderWidgetHostHWND") or (name.startswith("Qt") and name.endswith("QWindowIcon"))


def probe_client_window(process_id: int) -> WindowProbe:
    """The splash's readiness rule: a visible window of the client whose page surface shows real content."""

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback, wintypes.LPARAM]
    user32.EnumChildWindows.argtypes = [wintypes.HWND, callback, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    for name in ("IsWindowVisible", "IsIconic", "IsHungAppWindow"):
        getattr(user32, name).argtypes = [wintypes.HWND]
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]

    class BitmapInfoHeader(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BitmapInfo(ctypes.Structure):
        _fields_ = [("header", BitmapInfoHeader), ("colours", wintypes.DWORD * 3)]

    def rect(handle) -> tuple[int, int]:
        value = wintypes.RECT()
        if not user32.GetWindowRect(handle, ctypes.byref(value)):
            return 0, 0
        return value.right - value.left, value.bottom - value.top

    def colours_of(handle, width: int, height: int) -> int:
        if width <= 0 or height <= 0 or width * height > 64_000_000:
            return 0
        screen = user32.GetDC(None)
        if not screen:
            return 0
        memory = gdi32.CreateCompatibleDC(screen)
        bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
        try:
            if not memory or not bitmap:
                return 0
            previous = gdi32.SelectObject(memory, bitmap)
            printed = user32.PrintWindow(handle, memory, 2)
            gdi32.SelectObject(memory, previous)
            if not printed:
                return 0
            info = BitmapInfo()
            info.header.biSize = ctypes.sizeof(BitmapInfoHeader)
            info.header.biWidth, info.header.biHeight = width, -height  # top-down rows
            info.header.biPlanes, info.header.biBitCount = 1, 32
            pixels = ctypes.create_string_buffer(width * height * 4)
            if gdi32.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(info), 0) != height:
                return 0
            return count_colours(memoryview(pixels).cast("B"), width, height)
        finally:
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if memory:
                gdi32.DeleteDC(memory)
            user32.ReleaseDC(None, screen)

    best = WindowProbe()

    @callback
    def top_level(handle, _parameter):
        nonlocal best
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value != process_id or not user32.IsWindowVisible(handle) or user32.IsIconic(handle) \
                or user32.IsHungAppWindow(handle):
            return True
        width, height = rect(handle)
        if width < 600 or height < 400:
            return True
        if not best.window:
            best = WindowProbe(window=True)

        @callback
        def child(inner, _value):
            nonlocal best
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(inner, name, 256)
            inner_width, inner_height = rect(inner)
            if (user32.IsWindowVisible(inner) and inner_width >= width * 0.8 and inner_height >= height * 0.8
                    and _page_surface_class(name.value)):
                colours = colours_of(inner, inner_width, inner_height)
                if not best.surface or colours > best.colours:
                    best = WindowProbe(window=True, surface=True, colours=colours)
            return not best.painted

        user32.EnumChildWindows(handle, child, 0)
        return not best.painted

    user32.EnumWindows(top_level, 0)
    return best


# --- watcher ----------------------------------------------------------------------------------------------------

class ClientDisplayWatch:
    """Follow one client start and decide whether its window painted.

    The page counts as ready at the first marker newer than the client start, or ``page_ready_after_window``
    seconds after the window appeared. Unpainted ``unpainted_after`` seconds after that, the watch asks
    ``fallback`` for a compat action: with one it ends the client it was given (that process only) and leaves
    ``restart_action`` for the launcher; without one it logs ``client_unpainted`` and keeps watching.
    """

    def __init__(self, *, data_root: Path, log: Callable[..., None],
                 fallback: Callable[[], str | None] | None = None,
                 on_outcome: Callable[[bool], None] | None = None,
                 details: Mapping[str, object] | None = None,
                 probe: Callable[[int], WindowProbe] | None = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], object] | None = None,
                 interval: float = 1.0, unpainted_after: float = 20.0, page_ready_after_window: float = 30.0,
                 give_up_after: float = 180.0) -> None:
        self.data_root = Path(data_root)
        self._log = log
        self._fallback = fallback
        self._on_outcome = on_outcome
        self._details = dict(details or {})
        self._probe = probe or SYSTEM.probe or probe_client_window
        self._clock, self._wall = clock, wall
        self._stop = Event()
        self._sleep = sleep or self._stop.wait
        self.interval, self.unpainted_after = interval, unpainted_after
        self.page_ready_after_window, self.give_up_after = page_ready_after_window, give_up_after
        self.restart_action: str | None = None
        self.fields: dict[str, object] = {}
        self.status = "watching"
        self._thread: Thread | None = None

    def start(self, process: object) -> None:
        self._thread = Thread(target=self._guarded, args=(process,), name="olivia-client-display", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _guarded(self, process: object) -> None:
        try:
            self.run(process)
        except Exception as exc:  # A diagnostic watch can never end the client or the launcher.
            self.status = "failed"
            self._log("client_display_watch_failed", exception_type=type(exc).__name__)

    def _report(self, outcome: bool) -> None:
        if self._on_outcome is not None:
            try:
                self._on_outcome(outcome)
            except (OSError, ValueError, TypeError):
                pass

    def run(self, process: object) -> str:
        process_id = int(getattr(process, "pid", 0) or 0)
        poll = getattr(process, "poll", None)
        started, started_wall = self._clock(), self._wall()
        window_at: float | None = None
        marker_at: float | None = None
        reported = False
        last = WindowProbe()
        while not self._stop.is_set():
            if callable(poll) and poll() is not None:
                self.status = "exited"
                return self.status
            last = self._probe(process_id)
            now = self._clock()
            if last.painted:
                self.status = "painted"
                self._log("client_painted", elapsed_seconds=round(now - started, 3), colours=last.colours,
                          late=reported)
                self._report(True)
                return self.status
            if last.window and window_at is None:
                window_at = now
            if marker_at is None and page_ready_since(self.data_root, started_wall):
                marker_at = now
            if window_at is None:
                if now - started >= self.give_up_after and not reported:
                    reported = True
                    self._log("client_unpainted", reason="no_window", elapsed_seconds=round(now - started, 3),
                              **self._details)
                    self.status = "no_window"
                    return self.status
            elif not reported:
                assumed = window_at + self.page_ready_after_window
                ready = assumed if marker_at is None else min(max(window_at, marker_at), assumed)
                if now - ready >= self.unpainted_after:
                    reported = True
                    self.fields = {
                        "reason": "window_unpainted", "elapsed_seconds": round(now - started, 3),
                        "unpainted_seconds": round(now - ready, 3), "window_seconds": round(now - window_at, 3),
                        "page_ready": "marker" if marker_at is not None and marker_at <= assumed else "assumed",
                        "surface": last.surface, "colours": last.colours, **self._details,
                    }
                    action = self._fallback() if self._fallback is not None else None
                    if action:
                        self.restart_action = action
                        self.status = "restart"
                        terminate = getattr(process, "terminate", None)
                        if callable(terminate):
                            try:
                                terminate()
                            except OSError:
                                pass
                        return self.status
                    self._log("client_unpainted", **{**self.fields, "next_action": "none"})
                    self._report(False)
            elif now - started >= self.give_up_after:
                self.status = "unpainted"
                return self.status
            self._sleep(self.interval)
        self.status = "stopped" if self.status == "watching" else self.status
        return self.status


__all__ = [
    "ALIGN_ADAPTERS", "COMPAT_ACTIONS", "CONFIG_NAME", "ClientDisplayWatch", "CompatAction", "DisplayContext",
    "DisplayPlan", "DisplaySystem", "MODES", "PAGE_READY_NAME", "STATE_NAME", "SYSTEM", "WindowProbe",
    "count_colours", "count_display_adapters", "fallback_action", "find_entry", "helper_executable",
    "mark_page_ready", "normalize_exe_path", "page_ready_since", "parse_gpu_preference", "prepare_display",
    "probe_client_window", "read_mode", "record_outcome", "remember_fallback", "save_mode", "wanted_actions",
]
