"""Run the existing verified Mem0 embedding installer from Windows setup."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mem0_embedding_install import EmbeddingInstallResult, Mem0EmbeddingInstaller
from mem0_memory import Mem0Config, verified_embedding_cache


InstallerFactory = Callable[[Mem0Config], Mem0EmbeddingInstaller]


def provision_embedding(
    *,
    memory_root: Path,
    embedding_cache: Path,
    installer_factory: InstallerFactory = Mem0EmbeddingInstaller,
) -> EmbeddingInstallResult:
    if not memory_root.is_absolute() or not embedding_cache.is_absolute():
        raise ValueError("memory and embedding paths must be absolute")
    config = Mem0Config(
        enabled=True,
        data_root=memory_root,
        embedding_cache=embedding_cache,
    )
    return installer_factory(config).install()


def _emit(status: str, reason_code: str | None = None) -> None:
    payload: dict[str, str] = {"status": status}
    if reason_code is not None:
        payload["reason_code"] = reason_code
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-root", type=Path, required=True)
    parser.add_argument("--embedding-cache", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--install", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = Mem0Config(
            enabled=True,
            data_root=args.memory_root,
            embedding_cache=args.embedding_cache,
        )
        if args.verify_only:
            if not verified_embedding_cache(config):
                _emit("UNAVAILABLE", "MEM0_EMBEDDING_CACHE_UNAVAILABLE")
                return 2
            _emit("READY")
            return 0
        result = provision_embedding(
            memory_root=args.memory_root,
            embedding_cache=args.embedding_cache,
        )
        if result.status in {"APPLIED", "NOOP"} and verified_embedding_cache(config):
            _emit("READY")
            return 0
        _emit("UNAVAILABLE", result.reason_code or "MEM0_EMBEDDING_INSTALL_FAILED")
        return 2
    except (OSError, RuntimeError, TypeError, ValueError):
        _emit("UNAVAILABLE", "MEM0_EMBEDDING_INSTALL_FAILED")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
