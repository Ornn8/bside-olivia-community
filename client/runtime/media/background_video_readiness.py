"""Single-flight runtime checks that never hold the installer's status lock."""

from collections.abc import Callable, Mapping
from copy import deepcopy
import hashlib
from pathlib import Path
import threading
import time


class BackgroundVideoReadiness:
    def __init__(self, probe: Callable[[Mapping[str, str]], Mapping[str, object]],
                 *, fingerprint_paths: tuple[Path, ...] = (),
                 ttl_seconds: float = 600,
                 clock: Callable[[], float] = time.monotonic):
        self._probe = probe
        self._paths = fingerprint_paths
        self._ttl = ttl_seconds
        self._clock = clock
        self._completed_at = 0.0
        self._lock = threading.Lock()
        self._running = False
        self._result = None
        self._identity = None

    @staticmethod
    def _file_identity(path: Path) -> tuple:
        try:
            stat = path.stat()
            if path.is_dir():
                return (str(path), "directory")
            if path.suffix.lower() == ".json" and stat.st_size <= 1024 * 1024:
                return (str(path), hashlib.sha256(path.read_bytes()).digest())
            return (str(path), stat.st_size, stat.st_mtime_ns)
        except OSError:
            return (str(path), "missing")

    def _fingerprint(self, environment: Mapping[str, str]) -> tuple:
        paths = set(self._paths)
        paths.update(Path(value) for value in environment.values()
                     if isinstance(value, str) and Path(value).is_absolute())
        data_root = environment.get("OLIVIA_LOCAL_DATA_ROOT", "")
        if data_root and Path(data_root).is_absolute():
            paths.update(
                Path(data_root) / "capabilities" / "video" / bundle / ".ready.json"
                for bundle in ("ordinary_video", "music_video")
            )
        return (tuple(sorted(environment.items())),
                tuple(self._file_identity(path) for path in sorted(paths)))

    def __call__(self, environment: Mapping[str, str]) -> dict[str, object]:
        identity = self._fingerprint(environment)
        with self._lock:
            if (self._result is not None and self._identity == identity
                    and self._clock() - self._completed_at < self._ttl):
                return deepcopy(self._result)
            if not self._running:
                self._running = True
                threading.Thread(target=self._run, args=(dict(environment), identity),
                                 name="olivia-video-readiness", daemon=True).start()
        return {"ready": False, "music_ready": False,
                "ordinary_missing_dependencies": ["runtime_probe_pending"],
                "runtime_probe_pending": True}

    def _run(self, environment: Mapping[str, str], identity: tuple) -> None:
        try:
            result = dict(self._probe(environment))
        except Exception:
            result = {"ready": False, "music_ready": False,
                      "ordinary_missing_dependencies": ["runtime_probe_failed"]}
        with self._lock:
            self._result = result
            self._identity = identity
            self._completed_at = self._clock()
            self._running = False
