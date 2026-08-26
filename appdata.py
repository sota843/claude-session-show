"""App-local storage for the tray: a rolling log file and a small JSON state.

Under ``pythonw`` there is no console, so ``print`` output goes nowhere - every
failure the tray hit at login used to be invisible. Everything is mirrored to
``%LOCALAPPDATA%/ClaudeUsageTray/tray.log`` instead. The same directory holds
``state.json``, which survives restarts and carries the OAuth-refresh backoff
plus the last known-good usage snapshot.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any, Dict

APP_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "ClaudeUsageTray",
)
LOG_PATH = os.path.join(APP_DIR, "tray.log")
STATE_PATH = os.path.join(APP_DIR, "state.json")

_MAX_LOG_BYTES = 512 * 1024
_lock = threading.Lock()


def _ensure_dir() -> None:
    os.makedirs(APP_DIR, exist_ok=True)


def log(msg: str) -> None:
    """Timestamped line to the console (if any) and to the log file."""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001 - console may be absent/undecodable
        pass
    try:
        with _lock:
            _ensure_dir()
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > _MAX_LOG_BYTES:
                os.replace(LOG_PATH, LOG_PATH + ".1")
            with open(LOG_PATH, "a", encoding="utf-8", errors="backslashreplace") as fh:
                fh.write(line + "\n")
    except OSError:
        pass  # logging must never take the app down


def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: Dict[str, Any]) -> None:
    try:
        _ensure_dir()
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, STATE_PATH)
    except OSError as exc:  # noqa: BLE001
        print(f"[appdata] cannot write state: {exc}")


def update_state(**kwargs: Any) -> Dict[str, Any]:
    """Merge top-level keys into state.json."""
    with _lock:
        state = load_state()
        state.update(kwargs)
        save_state(state)
    return state
