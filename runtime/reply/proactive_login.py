"""Public settings hook for the windowless proactive login worker."""

from installer.proactive_login import (  # noqa: F401
    CHECK_INTERVAL_SECONDS,
    RUN_KEY,
    RUN_VALUE_NAME,
    configure_login_start,
    main,
    register_windows_login,
    run,
    start_login_worker,
    unregister_windows_login,
    windows_login_command,
)

__all__ = [
    "CHECK_INTERVAL_SECONDS",
    "RUN_KEY",
    "RUN_VALUE_NAME",
    "configure_login_start",
    "main",
    "register_windows_login",
    "run",
    "start_login_worker",
    "unregister_windows_login",
    "windows_login_command",
]


if __name__ == "__main__":
    raise SystemExit(main())
