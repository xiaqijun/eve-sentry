"""Per-user Windows startup registration."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def startup_command() -> str:
    """Return the command stored in the current user's Run key."""
    if getattr(sys, "frozen", False):
        return subprocess.list2cmdline([str(Path(sys.executable).resolve())])
    main_path = Path(__file__).resolve().parent.parent / "main.py"
    return subprocess.list2cmdline(
        [str(Path(sys.executable).resolve()), str(main_path)]
    )


def set_start_with_windows(enabled: bool) -> None:
    """Enable or disable per-user startup without requiring elevation."""
    if sys.platform != "win32":
        return
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        key_path,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        if enabled:
            winreg.SetValueEx(
                key,
                "EVE Sentry",
                0,
                winreg.REG_SZ,
                startup_command(),
            )
        else:
            try:
                winreg.DeleteValue(key, "EVE Sentry")
            except FileNotFoundError:
                pass
