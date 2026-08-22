"""Small, fail-closed adapters for optional later tranche scope children."""

from __future__ import annotations

from importlib import import_module
import os
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from types import ModuleType
from typing import Any, Callable, Sequence


_B11_MODULE = "tools.verify_b11_scope"
_SCOPE_CI_DIFF_ENABLED: ContextVar[bool] = ContextVar("scope_ci_diff_enabled", default=True)


@contextmanager
def scope_ci_diff_mode(enabled: bool):
    token = _SCOPE_CI_DIFF_ENABLED.set(enabled)
    try:
        yield
    finally:
        _SCOPE_CI_DIFF_ENABLED.reset(token)


def effective_scope_base(
    fallback: str,
    head: str = "HEAD",
    *,
    use_ci_diff: bool = True,
    resolver: Callable[..., Sequence[str]] | None = None,
) -> str:
    """Use CI's valid PR base to discover clean-checkout current paths.

    A fixed tranche baseline remains the ancestry contract.  ``DIFF_BASE``
    narrows only the current PR change-set, and an absent, invalid, or
    non-ancestor value falls back rather than widening ownership.
    """

    if not use_ci_diff or not _SCOPE_CI_DIFF_ENABLED.get():
        return fallback
    candidate = os.environ.get("DIFF_BASE", "").strip()
    if not candidate or set(candidate) == {"0"}:
        return fallback
    try:
        if resolver is not None:
            resolved = resolver("rev-parse", candidate)[0]
            resolved_head = resolver("rev-parse", head)[0]
            merge_base = resolver("merge-base", resolved, head)[0]
        else:
            resolved = subprocess.run(
                ["git", "rev-parse", candidate], check=True, capture_output=True,
                text=True, encoding="utf-8",
            ).stdout.strip()
            resolved_head = subprocess.run(
                ["git", "rev-parse", head], check=True, capture_output=True,
                text=True, encoding="utf-8",
            ).stdout.strip()
            merge_base = subprocess.run(
                ["git", "merge-base", resolved, head], check=True, capture_output=True,
                text=True, encoding="utf-8",
            ).stdout.strip()
    except (IndexError, OSError, subprocess.CalledProcessError):
        return fallback
    return resolved if merge_base == resolved and resolved != resolved_head else fallback


def _docs_b11_paths() -> frozenset[str]:
    """Return current-main's exact B11 documentation ownership candidate."""

    try:
        from tools.check_b11_docs import B11_OWNED_PATHS
    except (ImportError, AttributeError):
        return frozenset()
    return frozenset(str(path).replace("\\", "/") for path in B11_OWNED_PATHS)


def _verified_docs_b11_paths() -> tuple[frozenset[str], bool]:
    """Fail closed if the already-merged B11 documentation contract regresses."""

    try:
        from tools.check_b11_docs import verified_b11_paths

        paths, failed = verified_b11_paths()
    except Exception:
        return frozenset(), True
    return frozenset(str(path).replace("\\", "/") for path in paths), bool(failed)


def _b11_module() -> ModuleType | None:
    """Load B11 only when the visual tranche is present in this checkout."""

    try:
        return import_module(_B11_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name == _B11_MODULE:
            return None
        raise


def current_b11_paths() -> frozenset[str]:
    docs_paths = _docs_b11_paths()
    module = _b11_module()
    if module is None:
        return docs_paths
    getter = getattr(module, "current_b11_paths", None)
    if not callable(getter):
        return docs_paths
    return docs_paths | frozenset(str(path).replace("\\", "/") for path in getter())


def b11_child_exclusions(extra: frozenset[str] = frozenset()) -> frozenset[str]:
    """Build the explicit sibling set needed by a B11 child invocation."""

    from tools.verify_B07_scope import current_b07_paths
    from tools.verify_B10A_scope import current_b10a_paths
    from tools.verify_b05_scope import current_b05_paths
    from tools.verify_b06_scope import current_b06_paths
    from tools.verify_b08_scope import current_b08_paths
    from tools.verify_b10b_scope import current_b10b_paths
    from tools.verify_gov_scope import current_gov_paths
    from tools.verify_p01_scope import current_p01_paths

    return (
        frozenset(extra)
        | current_b05_paths()
        | current_b06_paths()
        | current_b07_paths()
        | current_b08_paths()
        | current_b10a_paths()
        | current_b10b_paths()
        | current_gov_paths()
        | current_p01_paths()
    )


def b02_child_exclusions(extra: frozenset[str] = frozenset()) -> frozenset[str]:
    """Build sibling exclusions for a non-recursive B02 contract child."""

    from tools.verify_B07_scope import current_b07_paths
    from tools.verify_B10A_scope import current_b10a_paths
    from tools.verify_b05_scope import current_b05_paths
    from tools.verify_b06_scope import current_b06_paths
    from tools.verify_b08_scope import current_b08_paths
    from tools.verify_b10b_scope import current_b10b_paths
    from tools.verify_gov_scope import current_gov_paths
    from tools.verify_p01_scope import current_p01_paths

    return (
        frozenset(extra)
        | current_b05_paths()
        | current_b06_paths()
        | current_b07_paths()
        | current_b08_paths()
        | current_b10a_paths()
        | current_b10b_paths()
        | current_gov_paths()
        | current_p01_paths()
        | current_b11_paths()
    )


def verified_b02_paths(
    excluded: frozenset[str] = frozenset(),
) -> tuple[frozenset[str], bool]:
    """Donate current B02 contract paths only after its child verifier passes."""

    try:
        from tools.verify_b02_scope import check_scope, current_b02_paths

        candidates = current_b02_paths()
        report = check_scope(
            excluded=b02_child_exclusions(frozenset(excluded)) - candidates,
            child_mode=True,
        )
    except Exception:
        return frozenset(), True
    if report.get("status") != "PASS":
        return frozenset(), True
    return candidates, False


def verified_b11_paths(
    excluded: frozenset[str] = frozenset(),
) -> tuple[frozenset[str], bool]:
    """Return B11-owned paths only after its independent child check passes."""

    docs_paths, docs_failed = _verified_docs_b11_paths()
    if docs_failed:
        return frozenset(), True
    module = _b11_module()
    if module is None:
        return docs_paths, False
    checker = getattr(module, "check_scope", None)
    if not callable(checker):
        return frozenset(), True
    baseline = getattr(module, "B11_BASELINE", None)
    if not isinstance(baseline, str) or not baseline:
        return frozenset(), True
    comparison_base = effective_scope_base(baseline)
    try:
        report = checker(
            base=comparison_base,
            excluded=frozenset(excluded),
            child_mode=True,
        )
    except TypeError:
        # A legacy checker is not composition-safe: calling it without child_mode
        # would re-enter the parent graph and hide ownership.
        return frozenset(), True
    except Exception:
        return frozenset(), True
    if not isinstance(report, dict) or report.get("status") != "PASS":
        return frozenset(), True
    owned = report.get("scope_paths")
    if owned is None:
        owned = current_b11_paths()
    return docs_paths | frozenset(str(path).replace("\\", "/") for path in owned), False


def invoke_legacy_compatible_child(
    checker: Callable[..., dict[str, Any]],
    *,
    excluded: frozenset[str],
    child_mode: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Invoke a child with the explicit protocol, preserving TypeError detail.

    This helper is intended for tests and adapters around overlap checkers. It
    never retries a legacy signature without child_mode; doing so would make
    recursive composition look like a successful child check.
    """

    try:
        report = checker(excluded=excluded, child_mode=child_mode, **kwargs)
    except TypeError as exc:
        return {
            "status": "FAIL",
            "error_code": "SCOPE_CHILD_SIGNATURE",
            "error_type": type(exc).__name__,
        }
    if not isinstance(report, dict) or report.get("status") != "PASS":
        return {"status": "FAIL", "child": report}
    return report
