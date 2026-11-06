"""Graphics adapter alignment for the client's two drawing programs, the unpainted-window watch and the splash limits.

Every test uses an in-memory registry, a fake adapter list and a scripted window probe: nothing here reads or
writes the real registry, inspects real windows or starts the native client.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from installer import display_compat as dc
from installer import full_patch


CLIENT = Path(r"F:\Games\Olivia\app\0.0.9.627\Olivia.exe")
MAIN = r"F:\Games\Olivia\app\0.0.9.627\Olivia.exe"
HELPER = r"F:\Games\Olivia\app\0.0.9.627\CefView\Olivia.exe"
IGPU = "PCI\\VEN_1002&DEV_1638&SUBSYS_13D21043"
DGPU = "PCI\\VEN_10DE&DEV_249D&SUBSYS_13D21043"


class Store:
    def __init__(self, values=None):
        self.data = dict(values or {})
        self.changes = []

    def values(self):
        return dict(self.data)

    def write(self, name, data):
        self.changes.append(("write", name, data))
        self.data[name] = data

    def delete(self, name):
        self.changes.append(("delete", name))
        self.data.pop(name, None)


def _system(store, adapters=(IGPU, DGPU), luid=None):
    return dc.DisplaySystem(store=lambda: store, device_ids=lambda: None if adapters is None else list(adapters),
                            probe=lambda _pid: dc.WindowProbe(), watch=False, adapter_luid=lambda _preference: luid)


def _mode(config, mode):
    dc.save_mode(config, mode)


# --- registry values -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("data, expected", [
    ("GpuPreference=1;", 1),
    ("GpuPreference=2;", 2),
    ("GpuPreference=0;", 0),
    ("SwapEffectUpgradeEnable=1;GpuPreference=2;", 2),
    ("GpuPreference=1;SwapEffectUpgradeEnable=1;AutoHDREnable=1;", 1),
    (" gpupreference = 1 ;", 1),
    ("GpuPreference=1", 1),
    ("GpuPreference=3;", None),
    ("GpuPreference=;", None),
    ("SwapEffectUpgradeEnable=1;", None),
    ("", None),
    (None, None),
    (5, None),
])
def test_the_gpu_preference_is_read_among_other_keys(data, expected):
    assert dc.parse_gpu_preference(data) == expected


def test_value_names_match_whatever_the_case_or_slashes():
    values = {"f:/games/OLIVIA/app/0.0.9.627//olivia.EXE": "GpuPreference=1;", "C:\\Other\\Olivia.exe": "GpuPreference=2;"}
    assert dc.find_entry(values, CLIENT) == ("f:/games/OLIVIA/app/0.0.9.627//olivia.EXE", "GpuPreference=1;")
    assert dc.find_entry(values, Path(r"F:\Games\Olivia\app\0.0.9.627\CefView\Olivia.exe")) is None
    assert dc.find_entry({}, CLIENT) is None
    assert dc.normalize_exe_path('"F:/A/../B/Olivia.exe"') == dc.normalize_exe_path(r"f:\b\OLIVIA.exe")


def test_display_adapters_are_the_pci_entries():
    assert dc.count_display_adapters([IGPU, DGPU.lower(), "ROOT\\BasicDisplay", "SWD\\RemoteDisplayEnum", 7]) == 2
    assert dc.count_display_adapters([IGPU]) == 1
    assert dc.count_display_adapters([]) == 0
    assert dc.count_display_adapters(None) is None


def test_the_mode_defaults_to_auto_and_rejects_other_values(tmp_path):
    assert dc.read_mode(tmp_path) == "auto"
    assert dc.save_mode(tmp_path, " ON ") == "on"
    assert json.loads((tmp_path / dc.CONFIG_NAME).read_text(encoding="utf-8")) == {"mode": "on", "schema_version": 1}
    assert dc.read_mode(tmp_path) == "on"
    for value in ("fast", "", None, 1, ["on"]):
        with pytest.raises(ValueError):
            dc.save_mode(tmp_path, value)
    (tmp_path / dc.CONFIG_NAME).write_text("not json", encoding="utf-8")
    assert dc.read_mode(tmp_path) == "auto"


# --- decision matrix -------------------------------------------------------------------------------------------

def _context(mode, preference, adapters, remembered=(), helper=None, ours=False):
    return dc.DisplayContext(mode=mode, client=dc.normalize_exe_path(MAIN), main_preference=preference,
                             helper_name=HELPER, helper_data=helper, helper_ours=ours, adapters=adapters,
                             remembered=tuple(remembered))


@pytest.mark.parametrize("mode", ["auto", "on", "off"])
@pytest.mark.parametrize("preference", [None, 0, 1, 2])
@pytest.mark.parametrize("adapters", [None, 1, 2, 3])
@pytest.mark.parametrize("remembered", [False, True])
def test_decision_matrix(mode, preference, adapters, remembered):
    context = _context(mode, preference, adapters, [dc.ALIGN_ADAPTERS] if remembered else [])
    chosen = preference in (1, 2)
    expected = chosen and (mode == "on" or (mode == "auto" and ((adapters or 0) >= 2 or remembered)))
    assert dc.wanted_actions(context) == ((dc.ALIGN_ADAPTERS,) if expected else ())


@pytest.mark.parametrize("mode", ["auto", "on"])
def test_a_user_entry_for_the_drawing_program_is_never_overridden(mode):
    assert dc.wanted_actions(_context(mode, 1, 2, helper="GpuPreference=2;", ours=False)) == ()
    assert dc.wanted_actions(_context(mode, 1, 2, helper="GpuPreference=2;", ours=True)) == (dc.ALIGN_ADAPTERS,)


# --- writing and undoing ---------------------------------------------------------------------------------------

def test_auto_with_two_adapters_mirrors_the_main_choice_and_records_it(tmp_path):
    store = Store({MAIN: "SwapEffectUpgradeEnable=1;GpuPreference=1;"})
    plan = dc.prepare_display(CLIENT, tmp_path, system=_system(store), now=100.0)
    assert store.data == {MAIN: "SwapEffectUpgradeEnable=1;GpuPreference=1;", HELPER: "GpuPreference=1;"}
    assert plan.applied == (dc.ALIGN_ADAPTERS,) and plan.switches == ()
    assert plan.fields == {"mode": "auto", "main_preference": 1, "helper_preference_before": None,
                           "helper_preference_after": 1, "helper_owner": "none", "adapters": 2,
                           "action": "align_written", "remembered": False, "applied": "align_adapters",
                           "adapter_luid": None}
    state = json.loads((tmp_path / dc.STATE_NAME).read_text(encoding="utf-8"))
    assert state["written"] == {"path": HELPER, "value": "GpuPreference=1;", "mirrors": 1,
                                "client": dc.normalize_exe_path(MAIN), "at": 100.0}
    again = dc.prepare_display(CLIENT, tmp_path, system=_system(store))
    assert again.fields["action"] == "align_kept" and again.fields["helper_owner"] == "launcher"
    assert store.changes == [("write", HELPER, "GpuPreference=1;")]


def test_the_helper_name_follows_the_main_entry_spelling(tmp_path):
    spelled = r"f:\games\olivia\APP\0.0.9.627\olivia.exe"
    store = Store({spelled: "GpuPreference=2;"})
    dc.prepare_display(CLIENT, tmp_path, system=_system(store))
    assert store.data[r"f:\games\olivia\APP\0.0.9.627\CefView\Olivia.exe"] == "GpuPreference=2;"


def test_our_entry_follows_the_main_choice_and_is_removed_when_it_goes(tmp_path):
    store = Store({MAIN: "GpuPreference=1;"})
    dc.prepare_display(CLIENT, tmp_path, system=_system(store))
    store.data[MAIN] = "GpuPreference=2;"
    plan = dc.prepare_display(CLIENT, tmp_path, system=_system(store))
    assert store.data[HELPER] == "GpuPreference=2;" and plan.fields["helper_preference_before"] == 1
    store.data[MAIN] = "GpuPreference=0;"
    plan = dc.prepare_display(CLIENT, tmp_path, system=_system(store))
    assert HELPER not in store.data and plan.fields["action"] == "align_removed"
    assert not (tmp_path / dc.STATE_NAME).exists()
    store.data[MAIN] = "GpuPreference=1;"
    dc.prepare_display(CLIENT, tmp_path, system=_system(store))
    del store.data[MAIN]
    dc.prepare_display(CLIENT, tmp_path, system=_system(store))
    assert store.data == {}


def test_off_never_writes_and_undoes_our_entry(tmp_path):
    store = Store({MAIN: "GpuPreference=1;"})
    dc.prepare_display(CLIENT, tmp_path, system=_system(store))
    _mode(tmp_path, "off")
    plan = dc.prepare_display(CLIENT, tmp_path, system=_system(store))
    assert store.data == {MAIN: "GpuPreference=1;"} and plan.fields["action"] == "align_removed"
    plan = dc.prepare_display(CLIENT, tmp_path, system=_system(store))
    assert store.data == {MAIN: "GpuPreference=1;"} and plan.applied == () and plan.fields["action"] == "none"


def test_on_aligns_even_with_one_adapter_and_auto_does_not(tmp_path):
    store = Store({MAIN: "GpuPreference=1;"})
    dc.prepare_display(CLIENT, tmp_path, system=_system(store, adapters=[IGPU]))
    assert HELPER not in store.data
    _mode(tmp_path, "on")
    dc.prepare_display(CLIENT, tmp_path, system=_system(store, adapters=None))
    assert store.data[HELPER] == "GpuPreference=1;"


@pytest.mark.parametrize("mode", ["auto", "on", "off"])
def test_a_user_entry_is_left_alone_in_every_mode(tmp_path, mode):
    store = Store({MAIN: "GpuPreference=1;", HELPER.lower(): "GpuPreference=2;SwapEffectUpgradeEnable=1;"})
    _mode(tmp_path, mode)
    plan = dc.prepare_display(CLIENT, tmp_path, system=_system(store))
    assert store.changes == [] and plan.fields["helper_owner"] == "user" and plan.fields["action"] == "none"
    assert dc.fallback_action(plan) is None


def test_an_entry_the_user_changed_after_us_becomes_theirs(tmp_path):
    store = Store({MAIN: "GpuPreference=1;"})
    dc.prepare_display(CLIENT, tmp_path, system=_system(store))
    store.data[HELPER] = "GpuPreference=2;"
    del store.data[MAIN]
    plan = dc.prepare_display(CLIENT, tmp_path, system=_system(store))
    assert store.data == {HELPER: "GpuPreference=2;"} and plan.fields["action"] == "none"
    assert not (tmp_path / dc.STATE_NAME).exists()


def test_a_moved_client_removes_the_old_entry_and_writes_the_new_one(tmp_path):
    moved = Path(r"D:\Olivia\app\0.0.9.627\Olivia.exe")
    store = Store({MAIN: "GpuPreference=1;", str(moved): "GpuPreference=1;"})
    dc.prepare_display(CLIENT, tmp_path, system=_system(store))
    dc.prepare_display(moved, tmp_path, system=_system(store))
    assert HELPER not in store.data
    assert store.data[r"D:\Olivia\app\0.0.9.627\CefView\Olivia.exe"] == "GpuPreference=1;"
    assert store.data[MAIN] == "GpuPreference=1;" and store.data[str(moved)] == "GpuPreference=1;"


def test_only_the_drawing_program_value_is_ever_changed(tmp_path):
    store = Store({MAIN: "GpuPreference=1;", r"C:\Other\Game.exe": "GpuPreference=2;"})
    for mode in ("auto", "on", "off", "on", "auto"):
        _mode(tmp_path, mode)
        dc.prepare_display(CLIENT, tmp_path, system=_system(store))
    assert {change[1] for change in store.changes} == {HELPER}
    assert store.data[MAIN] == "GpuPreference=1;" and store.data[r"C:\Other\Game.exe"] == "GpuPreference=2;"


# --- remembered fallback ---------------------------------------------------------------------------------------

def test_the_fallback_is_remembered_and_applied_directly_next_time(tmp_path):
    store = Store({MAIN: "GpuPreference=1;"})
    single = _system(store, adapters=[IGPU])
    plan = dc.prepare_display(CLIENT, tmp_path, system=single)
    assert plan.applied == () and dc.fallback_action(plan) == dc.ALIGN_ADAPTERS
    plan = dc.remember_fallback(CLIENT, tmp_path, dc.ALIGN_ADAPTERS, system=single, now=5.0)
    assert plan.applied == (dc.ALIGN_ADAPTERS,) and store.data[HELPER] == "GpuPreference=1;"
    assert dc.fallback_action(plan) is None  # nothing new for another restart
    memory = json.loads((tmp_path / dc.STATE_NAME).read_text(encoding="utf-8"))["remembered"]
    assert memory == {"actions": [dc.ALIGN_ADAPTERS], "preference": 1, "client": dc.normalize_exe_path(MAIN),
                      "good_starts": 0, "outcome": "pending", "at": 5.0}
    plan = dc.prepare_display(CLIENT, tmp_path, system=single)
    assert plan.applied == (dc.ALIGN_ADAPTERS,) and plan.fields["remembered"] is True


def test_the_memory_is_cleared_when_the_windows_choice_changes(tmp_path):
    store = Store({MAIN: "GpuPreference=1;"})
    single = _system(store, adapters=[IGPU])
    dc.remember_fallback(CLIENT, tmp_path, dc.ALIGN_ADAPTERS, system=single)
    store.data[MAIN] = "GpuPreference=2;"
    plan = dc.prepare_display(CLIENT, tmp_path, system=single)
    assert plan.applied == () and HELPER not in store.data and plan.fields["remembered"] is False
    assert not (tmp_path / dc.STATE_NAME).exists()


def test_the_memory_is_cleared_after_enough_good_starts(tmp_path):
    store = Store({MAIN: "GpuPreference=1;"})
    single = _system(store, adapters=[IGPU])
    plan = dc.remember_fallback(CLIENT, tmp_path, dc.ALIGN_ADAPTERS, system=single)
    dc.record_outcome(tmp_path, False, applied=plan.applied)
    memory = json.loads((tmp_path / dc.STATE_NAME).read_text(encoding="utf-8"))["remembered"]
    assert memory["outcome"] == "unpainted" and memory["good_starts"] == 0
    for count in range(1, dc.GOOD_STARTS_BEFORE_RECHECK):
        plan = dc.prepare_display(CLIENT, tmp_path, system=single)
        assert plan.applied == (dc.ALIGN_ADAPTERS,)
        dc.record_outcome(tmp_path, True, applied=plan.applied)
        assert json.loads((tmp_path / dc.STATE_NAME).read_text(encoding="utf-8"))["remembered"]["good_starts"] == count
    dc.record_outcome(tmp_path, True, applied=plan.applied)
    plan = dc.prepare_display(CLIENT, tmp_path, system=single)
    assert plan.applied == () and HELPER not in store.data


def test_good_starts_count_only_when_the_remembered_action_ran(tmp_path):
    store = Store({MAIN: "GpuPreference=1;"})
    dc.remember_fallback(CLIENT, tmp_path, dc.ALIGN_ADAPTERS, system=_system(store, adapters=[IGPU]))
    dc.record_outcome(tmp_path, True, applied=())
    memory = json.loads((tmp_path / dc.STATE_NAME).read_text(encoding="utf-8"))["remembered"]
    assert memory["good_starts"] == 0 and memory["outcome"] == "pending"


@pytest.mark.parametrize("mode", ["on", "off"])
def test_the_fallback_restart_is_auto_only(tmp_path, mode):
    store = Store({MAIN: "GpuPreference=1;"})
    _mode(tmp_path, mode)
    assert dc.fallback_action(dc.prepare_display(CLIENT, tmp_path, system=_system(store, adapters=[IGPU]))) is None


def test_no_fallback_without_a_main_choice(tmp_path):
    plan = dc.prepare_display(CLIENT, tmp_path, system=_system(Store(), adapters=[IGPU]))
    assert dc.fallback_action(plan) is None and plan.fields["main_preference"] is None


# --- page ready marker -----------------------------------------------------------------------------------------

def test_the_page_ready_marker_counts_only_after_the_client_start(tmp_path, monkeypatch):
    monkeypatch.delenv("OLIVIA_LOCAL_DATA_ROOT", raising=False)
    assert dc.mark_page_ready() is False
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    assert dc.mark_page_ready(now=50.0) is True
    assert dc.page_ready_since(tmp_path, 49.0) is True
    assert dc.page_ready_since(tmp_path, 51.0) is False
    (tmp_path / "logs" / dc.PAGE_READY_NAME).write_text("garbage", encoding="ascii")
    assert dc.page_ready_since(tmp_path, 0.0) is False


# --- colours ---------------------------------------------------------------------------------------------------

def _buffer(width, height, colour=lambda x, y: (0, 0, 0)):
    pixels = bytearray(width * height * 4)
    for y in range(height):
        for x in range(width):
            index = (y * width + x) * 4
            pixels[index:index + 3] = bytes(colour(x, y))
            pixels[index + 3] = (x * 7) % 256  # alpha never counts
    return pixels


def test_a_blank_surface_has_one_colour():
    assert dc.count_colours(_buffer(200, 200), 200, 200) == 1


def test_colours_are_sampled_on_the_splash_grid_only():
    width, height = 100, 140  # x 30, 47, 64; y 80, 97, 114
    grid = {(x, y) for x in (30, 47, 64) for y in (80, 97, 114)}
    pixels = _buffer(width, height, lambda x, y: ((x, y, 1) if (x, y) in grid else (9, 9, 9)))
    assert dc.count_colours(pixels, width, height) == 9
    assert dc.count_colours(pixels, width, height, stride=width * 4) == 9


def test_a_painted_page_reaches_the_threshold():
    pixels = _buffer(640, 420, lambda x, y: ((x * 3) % 256, (y * 5) % 256, 40))
    colours = dc.count_colours(pixels, 640, 420)
    assert colours >= dc.PAINTED_COLOURS
    assert dc.WindowProbe(window=True, surface=True, colours=colours).painted
    assert not dc.WindowProbe(window=True, surface=True, colours=dc.PAINTED_COLOURS - 1).painted


# --- the watch -------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Process:
    def __init__(self, exit_after=None, clock=None):
        self.pid, self.terminated, self._exit_after, self._clock = 4321, 0, exit_after, clock

    def poll(self):
        if self.terminated or (self._exit_after is not None and self._clock.now >= self._exit_after):
            return 1
        return None

    def terminate(self):
        self.terminated += 1


def _watch(tmp_path, clock, probe, *, fallback=lambda: dc.ALIGN_ADAPTERS, marker_at=None, wall_start=500.0):
    logged, outcomes = [], []

    def wall():
        return wall_start + (clock.now - 1000.0)

    def scripted(pid):
        assert pid == 4321
        if marker_at is not None and clock.now - 1000.0 >= marker_at and not (tmp_path / "logs" / dc.PAGE_READY_NAME).exists():
            dc.mark_page_ready(tmp_path, now=wall())
        return probe(clock.now - 1000.0)

    watch = dc.ClientDisplayWatch(data_root=tmp_path, log=lambda event, **fields: logged.append((event, fields)),
                                  fallback=fallback, on_outcome=outcomes.append, details={"mode": "auto", "adapters": 1},
                                  probe=scripted, clock=clock, wall=wall, sleep=clock.sleep)
    return watch, logged, outcomes


def _black_window_after(seconds):
    return lambda elapsed: dc.WindowProbe(window=elapsed >= seconds, surface=elapsed >= seconds, colours=1)


def test_a_window_that_paints_is_reported_once(tmp_path):
    clock = Clock()
    watch, logged, outcomes = _watch(tmp_path, clock, lambda elapsed: dc.WindowProbe(True, True, 40 if elapsed >= 6 else 1))
    process = Process()
    assert watch.run(process) == "painted"
    assert logged == [("client_painted", {"elapsed_seconds": 6.0, "colours": 40, "late": False})]
    assert outcomes == [True] and process.terminated == 0 and watch.restart_action is None


def test_an_unpainted_window_asks_for_one_compat_restart(tmp_path):
    clock = Clock()
    watch, logged, outcomes = _watch(tmp_path, clock, _black_window_after(4), marker_at=8)
    process = Process()
    assert watch.run(process) == "restart"
    assert process.terminated == 1 and watch.restart_action == dc.ALIGN_ADAPTERS
    assert watch.fields["reason"] == "window_unpainted" and watch.fields["page_ready"] == "marker"
    assert watch.fields["elapsed_seconds"] == 28.0  # page ready at 8 s, then 20 s unpainted
    assert watch.fields["window_seconds"] == 24.0 and watch.fields["mode"] == "auto"
    assert logged == [] and outcomes == []  # the launcher logs the restart with the action it applied


def test_without_a_new_action_it_only_logs_and_keeps_the_client(tmp_path):
    clock = Clock()
    probe = lambda elapsed: dc.WindowProbe(True, True, 30 if elapsed >= 70 else 1)
    watch, logged, outcomes = _watch(tmp_path, clock, probe, fallback=lambda: None, marker_at=2)
    process = Process()
    assert watch.run(process) == "painted"
    assert process.terminated == 0 and watch.restart_action is None
    assert [event for event, _ in logged] == ["client_unpainted", "client_painted"]
    unpainted = logged[0][1]
    assert unpainted["reason"] == "window_unpainted" and unpainted["next_action"] == "none"
    assert unpainted["elapsed_seconds"] == 22.0 and logged[1][1]["late"] is True  # page ready at 2 s
    assert outcomes == [False, True]


def test_without_a_marker_the_page_counts_as_ready_thirty_seconds_after_the_window(tmp_path):
    clock = Clock()
    watch, _logged, _outcomes = _watch(tmp_path, clock, _black_window_after(10))
    assert watch.run(Process()) == "restart"
    assert watch.fields["elapsed_seconds"] == 60.0 and watch.fields["page_ready"] == "assumed"


def test_a_marker_before_the_window_counts_from_the_window(tmp_path):
    clock = Clock()
    watch, _logged, _outcomes = _watch(tmp_path, clock, _black_window_after(10), marker_at=3)
    assert watch.run(Process()) == "restart"
    assert watch.fields["elapsed_seconds"] == 30.0


def test_a_late_marker_never_pushes_the_check_past_the_fallback(tmp_path):
    clock = Clock()
    watch, _logged, _outcomes = _watch(tmp_path, clock, _black_window_after(5), marker_at=45)
    assert watch.run(Process()) == "restart"
    assert watch.fields["elapsed_seconds"] == 55.0 and watch.fields["page_ready"] == "assumed"


def test_a_marker_from_before_this_start_is_ignored(tmp_path):
    clock = Clock()
    dc.mark_page_ready(tmp_path, now=100.0)  # an earlier start of the client
    watch, _logged, _outcomes = _watch(tmp_path, clock, _black_window_after(2))
    assert watch.run(Process()) == "restart"
    assert watch.fields["page_ready"] == "assumed" and watch.fields["elapsed_seconds"] == 52.0


def test_a_client_that_exits_ends_the_watch_quietly(tmp_path):
    clock = Clock()
    watch, logged, outcomes = _watch(tmp_path, clock, _black_window_after(2))
    assert watch.run(Process(exit_after=12, clock=clock)) == "exited"
    assert logged == [] and outcomes == []


def test_a_window_that_never_appears_is_logged_once(tmp_path):
    clock = Clock()
    watch, logged, _outcomes = _watch(tmp_path, clock, lambda _elapsed: dc.WindowProbe())
    assert watch.run(Process()) == "no_window"
    assert [(event, fields["reason"]) for event, fields in logged] == [("client_unpainted", "no_window")]


def test_the_watch_thread_never_holds_the_launcher(tmp_path):
    logged = []
    watch = dc.ClientDisplayWatch(data_root=tmp_path, log=lambda event, **fields: logged.append(event),
                                  probe=lambda _pid: (_ for _ in ()).throw(OSError("probe broke")), interval=0.01)
    watch.start(SimpleNamespace(pid=1, poll=lambda: None))
    watch._thread.join(5)
    watch.stop()
    assert watch.status == "failed" and logged == ["client_display_watch_failed"]
    idle = dc.ClientDisplayWatch(data_root=tmp_path, log=lambda *a, **k: None, probe=lambda _pid: dc.WindowProbe(),
                                 interval=0.01)
    idle.start(SimpleNamespace(pid=1, poll=lambda: None))
    idle.stop()
    assert idle.status == "stopped"


# --- launcher ---------------------------------------------------------------------------------------------------

def test_the_client_runner_starts_and_stops_the_watch_on_the_process_it_started(tmp_path, monkeypatch):
    from installer import start_local
    from installer.native_window_layout import LayoutStatus
    seen = []

    class Watch:
        def start(self, process):
            seen.append(("start", process.pid))

        def stop(self):
            seen.append(("stop",))

    commands = []
    monkeypatch.setattr(start_local.subprocess, "Popen",
                        lambda command, **_kwargs: commands.append(command) or SimpleNamespace(pid=77, wait=lambda: 5))
    monkeypatch.setattr(start_local.subprocess, "call", lambda *_a, **_k: pytest.fail("a watched client needs its pid"))
    monkeypatch.setattr(start_local, "guard_native_window_layout", lambda *a, **k: LayoutStatus.SKIPPED)
    monkeypatch.setattr(start_local, "_append_launcher_event", lambda *a, **k: None)
    assert start_local._run_client_with_native_layout(
        tmp_path / "Olivia.exe", tmp_path, cwd=tmp_path, environment={}, data_root=tmp_path, attempt=2,
        display_watch=Watch(), client_switches=("--example-switch",)) == 5
    assert seen == [("start", 77), ("stop",)]
    assert commands[0][-1] == "--example-switch"


def test_the_splash_outcome_is_logged_once(tmp_path):
    from installer import start_local
    report = tmp_path / "logs" / "startup-animation.json"
    report.parent.mkdir(parents=True)
    report.write_text("\ufeff" + json.dumps({"status": "timeout", "outcome": "timeout", "elapsed_seconds": 36.2,
                                              "ended_after_seconds": 6.1, "loaded": True, "ended": True,
                                              "error": "C:/Users/someone"}), encoding="utf-8")
    splash = start_local._StartupSplashReport(SimpleNamespace(poll=lambda: 0), report, tmp_path)
    splash.log()
    splash.finish(closed_by_launcher=False)
    events = [json.loads(line) for line in (tmp_path / "logs" / "launcher.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(events) == 1
    event = events[0]
    del event["timestamp"]
    assert event == {"event": "startup_splash", "status": "timeout", "stage": "timeout", "elapsed_seconds": 36.2,
                     "ended_after_seconds": 6.1, "loaded": True, "ended": True}


def test_a_splash_the_launcher_had_to_end_is_logged_as_such(tmp_path):
    from installer import start_local
    splash = start_local._StartupSplashReport(SimpleNamespace(poll=lambda: None), tmp_path / "missing.json", tmp_path)
    splash.finish(closed_by_launcher=True)
    event = json.loads((tmp_path / "logs" / "launcher.jsonl").read_text(encoding="utf-8"))
    assert event["event"] == "startup_splash" and event["status"] == "closed_by_launcher"


# --- splash script ----------------------------------------------------------------------------------------------

SPLASH = Path(__file__).resolve().parents[2] / "installer" / "startup_animation.ps1"


def _tick(script):
    return script[script.index("$timer.Add_Tick({"):script.index("$timer.Start()")]


def test_the_splash_closes_after_the_video_or_a_minute_even_without_a_painted_window():
    script = SPLASH.read_text(encoding="utf-8")
    assert re.search(r"\[double\]\$HoldAfterEndSeconds = 30\b", script)
    assert re.search(r"\[double\]\$MaxSeconds = 60\b", script)
    tick = _tick(script)
    limit = tick.index("Close-Splash 'timeout'")
    assert "$elapsed - $script:endedAt -ge $HoldAfterEndSeconds" in tick and "$elapsed -ge $MaxSeconds" in tick
    # the limit is checked before anything that waits for a client process id or a painted window
    assert limit < tick.index("if ($script:clientId -le 0) { return }") < tick.index("[OliviaStartupReady]::Begin")
    assert "$script:endedAt = $script:clock.Elapsed.TotalSeconds" in script


def test_the_painted_check_runs_off_the_ui_thread():
    script = SPLASH.read_text(encoding="utf-8")
    tick = _tick(script)
    assert "::Find(" not in tick
    assert "[OliviaStartupReady]::Begin($script:clientId)" in tick and "[OliviaStartupReady]::Result($script:clientId)" in tick
    assert "ThreadPool.QueueUserWorkItem" in script and "Find(pid)" in script


def test_a_click_or_escape_dismisses_the_splash():
    script = SPLASH.read_text(encoding="utf-8")
    click = script[script.index("Add_PreviewMouseLeftButtonUp"):]
    assert click.index("Close-Splash 'skipped'") < click.index("Add_Closed")
    assert "[Windows.Input.Key]::Escape" in script


def test_every_way_the_splash_ends_records_an_outcome():
    script = SPLASH.read_text(encoding="utf-8")
    for outcome in ("ready", "timeout", "skipped", "client_exited", "launcher_exited", "error"):
        assert f"Close-Splash '{outcome}'" in script
    assert "$script:report.outcome = $Outcome" in script and "$script:report.elapsed_seconds = Get-Elapsed" in script
    assert "Set-Outcome 'closed'" in script  # any other close
    assert "Keep the frame during a native-client retry" in script  # the client PID handshake stays


@pytest.mark.skipif(os.name != "nt" or shutil.which("powershell.exe") is None, reason="Windows PowerShell only")
def test_the_splash_writes_its_outcome_when_it_cannot_start(tmp_path):
    report = tmp_path / "report.json"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-File", str(SPLASH), "-MediaPath", str(tmp_path / "missing.mp4"), "-ReportPath", str(report)],
                            capture_output=True, timeout=120)
    assert result.returncode == 1
    values = json.loads(report.read_text(encoding="utf-8-sig"))
    assert values["outcome"] == "error" and values["status"] == "error"
    assert isinstance(values["elapsed_seconds"], (int, float)) and values["started_at"]


# --- payload --------------------------------------------------------------------------------------------------

def test_the_new_files_ship_in_the_patch_and_the_installer():
    from installer import build_windows_setup
    assert "original_client_display_api.py" in full_patch.PAYLOAD_ROOT_FILES
    assert "original_client_display_api.py" in full_patch.PAYLOAD_REQUIRED_ROOT_FILES
    assert "original_client_display_api.py" in build_windows_setup.RELEASE_ROOT_FILES
    assert "installer" in full_patch.PAYLOAD_DIRS  # installer/display_compat.py


# --- settings page and diagnostics ------------------------------------------------------------------------------

def test_the_setting_sits_in_updates_and_help_and_saves_through_the_route():
    from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT as script
    assert 'const DISPLAY_COMPAT_PATH = "/api/display-compat";' in script
    help_group = script[script.index('settingsGroup("help"'):script.index('settingsGroup("language"')]
    assert "mountDisplayCompatSetting(help);" in help_group
    block = script[script.index("const mountDisplayCompatSetting"):script.index("const mountShell")]
    assert '[["auto", t("自动")], ["on", t("开")], ["off", t("关")]]' in block
    assert 'setAttribute("role", "radiogroup")' in block and 'setAttribute("role", "radio")' in block
    assert "requestSetup(DISPLAY_COMPAT_PATH, { mode: value })" in block
    assert 't("已保存，下次启动 Olivia 时生效。")' in block


def test_the_diagnostic_bundle_keeps_only_bounded_display_fields():
    from runtime.diagnostics.support_bundle import _project_tail_record
    record = _project_tail_record({
        "event": "client_compat_restart", "attempt": 2, "reason": "window_unpainted", "elapsed_seconds": 28.0,
        "unpainted_seconds": 20.0, "window_seconds": 24.0, "page_ready": "marker", "mode": "auto",
        "main_preference": 1, "helper_preference_before": None, "adapters": 1, "applied": "none",
        "action": "align_adapters", "surface": True, "colours": 1, "helper_owner": "C:/Users/someone",
        "path": "F:/Games/Olivia"}, runtime=False)
    assert record == {
        "event": "client_compat_restart", "attempt": 2, "reason": "window_unpainted", "elapsed_seconds": 28.0,
        "unpainted_seconds": 20.0, "window_seconds": 24.0, "page_ready": "marker", "mode": "auto",
        "main_preference": 1, "adapters": 1, "applied": "none", "action": "align_adapters", "surface": True,
        "colours": 1}


# --- pinning the GPU process to the main program's adapter ------------------------------------------------------

HARDWARE = (0, 63804, 0)
WARP = (0, 85752, 2)  # DXGI_ADAPTER_FLAG_SOFTWARE


@pytest.mark.parametrize("preference, dxgi", [(1, 1), (2, 2)])
def test_the_windows_choice_maps_to_the_dxgi_preference(preference, dxgi):
    asked = []
    assert dc.read_adapter_luid(preference, adapters=lambda value: asked.append(value) or [HARDWARE]) == (0, 63804)
    assert asked == [dxgi]


@pytest.mark.parametrize("preference", [0, None, 3])
def test_no_adapter_is_looked_up_without_a_choice(preference):
    assert dc.read_adapter_luid(preference, adapters=lambda _value: pytest.fail("not asked")) is None


def test_software_adapters_are_skipped():
    assert dc.read_adapter_luid(1, adapters=lambda _value: [WARP, (-1, 4294967295, 0)]) == (-1, 4294967295)
    assert dc.read_adapter_luid(1, adapters=lambda _value: [WARP]) is None


@pytest.mark.parametrize("answer", [None, [], OSError("COM error")])
def test_a_failed_lookup_gives_no_adapter(answer):
    def adapters(_value):
        if isinstance(answer, Exception):
            raise answer
        return answer
    assert dc.read_adapter_luid(2, adapters=adapters) is None


def test_the_switch_text_is_decimal_high_then_low():
    assert dc.adapter_luid_switch((0, 63804)) == "--use-adapter-luid=0,63804"
    assert dc.adapter_luid_switch((-1, 4294967295)) == "--use-adapter-luid=-1,4294967295"


def _pin_system(store, adapters, luid=(0, 63804)):
    asked = []
    system = _system(store, adapters=adapters)
    system.adapter_luid = lambda preference: asked.append(preference) or luid
    return system, asked


PIN = ("--use-adapter-luid=0,63804",)


@pytest.mark.parametrize("mode, adapters, pinned", [
    ("auto", [IGPU, DGPU], True), ("auto", [IGPU], False), ("auto", None, False),
    ("on", [IGPU], True), ("on", None, True), ("off", [IGPU, DGPU], False)])
def test_pinning_follows_the_same_gating_as_the_alignment(tmp_path, mode, adapters, pinned):
    store = Store({MAIN: "GpuPreference=1;"})
    _mode(tmp_path, mode)
    system, asked = _pin_system(store, adapters)
    plan = dc.prepare_display(CLIENT, tmp_path, system=system)
    assert plan.switches == (PIN if pinned else ())
    assert plan.applied == ((dc.ALIGN_ADAPTERS, dc.PIN_ADAPTER) if pinned else ())
    assert (HELPER in store.data) is pinned
    assert asked == ([] if mode == "off" else [1])
    assert plan.fields["applied"] == ("align_adapters+pin_adapter" if pinned else "none")
    assert plan.fields["adapter_luid"] == (None if mode == "off" else "0,63804")


@pytest.mark.parametrize("main", [None, "GpuPreference=0;", "SwapEffectUpgradeEnable=1;"])
def test_no_switch_without_a_main_choice(tmp_path, main):
    store = Store({MAIN: main} if main else {})
    _mode(tmp_path, "on")
    system, asked = _pin_system(store, [IGPU, DGPU])
    plan = dc.prepare_display(CLIENT, tmp_path, system=system)
    assert plan.switches == () and asked == [] and plan.fields["adapter_luid"] is None


@pytest.mark.parametrize("luid", [None, (0,), ("0", "1"), (0, -1), (1 << 31, 0)])
def test_a_failed_or_odd_lookup_adds_no_switch_but_still_aligns(tmp_path, luid):
    store = Store({MAIN: "GpuPreference=2;"})
    system, _asked = _pin_system(store, [IGPU, DGPU], luid=luid)
    plan = dc.prepare_display(CLIENT, tmp_path, system=system)
    assert plan.switches == () and plan.applied == (dc.ALIGN_ADAPTERS,)
    assert store.data[HELPER] == "GpuPreference=2;" and plan.fields["adapter_luid"] is None


def test_the_restart_brings_every_missing_action_and_remembers_them(tmp_path):
    store = Store({MAIN: "GpuPreference=1;"})
    system, _asked = _pin_system(store, [IGPU])
    plan = dc.prepare_display(CLIENT, tmp_path, system=system)
    assert plan.applied == () and dc.fallback_action(plan) == "align_adapters+pin_adapter"
    plan = dc.remember_fallback(CLIENT, tmp_path, dc.fallback_action(plan), system=system)
    assert plan.applied == (dc.ALIGN_ADAPTERS, dc.PIN_ADAPTER) and plan.switches == PIN
    assert dc.fallback_action(plan) is None  # nothing new: the watch only logs client_unpainted
    again = dc.prepare_display(CLIENT, tmp_path, system=system)
    assert again.switches == PIN and again.fields["remembered"] is True


def test_a_user_entry_still_leaves_the_pin_for_the_restart(tmp_path):
    store = Store({MAIN: "GpuPreference=1;", HELPER: "GpuPreference=2;"})
    system, _asked = _pin_system(store, [IGPU])
    plan = dc.prepare_display(CLIENT, tmp_path, system=system)
    assert dc.fallback_action(plan) == dc.PIN_ADAPTER
    plan = dc.remember_fallback(CLIENT, tmp_path, dc.PIN_ADAPTER, system=system)
    assert plan.applied == (dc.PIN_ADAPTER,) and plan.switches == PIN
    assert store.data[HELPER] == "GpuPreference=2;"  # the user's own entry is untouched


def test_with_both_actions_in_place_there_is_no_restart(tmp_path):
    store = Store({MAIN: "GpuPreference=1;"})
    system, _asked = _pin_system(store, [IGPU, DGPU])
    plan = dc.prepare_display(CLIENT, tmp_path, system=system)
    assert plan.applied == (dc.ALIGN_ADAPTERS, dc.PIN_ADAPTER) and dc.fallback_action(plan) is None


def test_the_bundle_keeps_the_pinned_luid():
    from runtime.diagnostics.support_bundle import _project_tail_record
    record = _project_tail_record({"event": "client_display", "adapter_luid": "0,63804",
                                   "applied": "align_adapters+pin_adapter"}, runtime=False)
    assert record == {"event": "client_display", "adapter_luid": "0,63804", "applied": "align_adapters+pin_adapter"}
    assert "adapter_luid" not in _project_tail_record({"event": "client_display", "adapter_luid": "C:/x"}, runtime=False)
