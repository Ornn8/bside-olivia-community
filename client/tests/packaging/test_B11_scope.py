"""B11 scope boundary tests."""

from __future__ import annotations

from typing import Any

import tools.verify_b11_scope as b11_scope


def test_b11_scope_accepts_only_declared_paths() -> None:
    assert b11_scope.is_b11_path("runtime/visual/livetalking.py")
    assert b11_scope.is_b11_path("runtime/visual/livetalking_backend.py")
    assert b11_scope.is_b11_path("tools/livetalking_worker.py")
    assert not b11_scope.is_b11_path("random/unrelated.txt")
    import tools.verify_b08_scope as b08_scope

    expected = {
        "docs/B10B_MODULE_LIFECYCLE.md",
        "runtime/packaging/b10b/manager.py",
        "runtime/packaging/manifests/b10b.modules.json",
    }
    assert expected <= b11_scope.B11_SHARED_B10B
    assert expected <= b08_scope.B08_SHARED_B10B
    assert "runtime/packaging/b10a/manager.py" in b11_scope.B11_SHARED_B10A
    assert "docs/B11_VISUAL_RUNTIME.md" not in b11_scope.B11_SHARED_B10B
    assert "docs/B11_VISUAL_RUNTIME.md" not in b08_scope.B08_SHARED_B10B


def test_b11_scope_fails_closed_on_unrelated_dirty(monkeypatch: Any) -> None:
    def fake_git(*args: str) -> list[str]:
        if args[:2] == ("rev-parse", "HEAD"):
            return ["head"]
        if args[:2] == ("rev-parse", b11_scope.B11_BASELINE):
            return [b11_scope.B11_BASELINE]
        if args[:1] == ("merge-base",):
            return [b11_scope.B11_BASELINE]
        if args[:2] == ("diff", "--name-only"):
            return ["runtime/visual/livetalking.py"]
        return []

    monkeypatch.setattr(b11_scope, "_git", fake_git)
    monkeypatch.setattr(b11_scope, "_status_paths", lambda: ["random/unrelated.txt"])
    report = b11_scope.check_scope()
    assert report["status"] == "FAIL"
    assert report["unexpected_paths"] == ["random/unrelated.txt"]


def test_current_b11_paths_uses_the_valid_pr_base_in_ci(monkeypatch: Any) -> None:
    pr_base = "46a068a81a1059ac6b2bc89b20089ee8086f2719"
    seen: list[tuple[str, ...]] = []
    monkeypatch.setenv("DIFF_BASE", pr_base)

    def fake_git(*args: str) -> list[str]:
        if args[:1] == ("rev-parse",):
            return [args[1]]
        if args[:1] == ("merge-base",):
            return [args[1]]
        if args[:2] == ("diff", "--name-only"):
            seen.append(args)
            return ["runtime/visual/livetalking.py"]
        return []

    monkeypatch.setattr(b11_scope, "_git", fake_git)

    assert b11_scope.current_b11_paths() == frozenset({"runtime/visual/livetalking.py"})
    assert seen[0][2] == pr_base
