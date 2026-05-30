"""Windows 'run at login' toggle via a shortcut in the Startup folder.

Creates/removes ``ClaudeUsageTray.lnk`` in the user's Startup folder that
launches the tray app with ``pythonw.exe`` (no console window). Shortcut
creation uses the WScript.Shell COM object through PowerShell so there is no
extra pip dependency (pywin32 not required).
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP_SCRIPT = os.path.join(HERE, "tray_app.py")
SHORTCUT_NAME = "ClaudeUsageTray.lnk"


def _startup_dir() -> str:
    return os.path.join(
        os.environ["APPDATA"], "Microsoft", "Windows",
        "Start Menu", "Programs", "Startup",
    )


def _shortcut_path() -> str:
    return os.path.join(_startup_dir(), SHORTCUT_NAME)


def _pythonw() -> str:
    cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return cand if os.path.exists(cand) else sys.executable


def is_enabled() -> bool:
    return os.path.exists(_shortcut_path())


def enable() -> None:
    lnk = _shortcut_path()
    target = _pythonw()
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{lnk}'); "
        f"$s.TargetPath = '{target}'; "
        f"$s.Arguments = '\"{APP_SCRIPT}\"'; "
        f"$s.WorkingDirectory = '{HERE}'; "
        "$s.WindowStyle = 7; "
        "$s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        check=True, capture_output=True, text=True,
    )


def disable() -> None:
    lnk = _shortcut_path()
    if os.path.exists(lnk):
        os.remove(lnk)


def toggle() -> bool:
    if is_enabled():
        disable()
    else:
        enable()
    return is_enabled()


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "enable":
        enable()
    elif action == "disable":
        disable()
    print("autostart enabled:", is_enabled(), "->", _shortcut_path())
