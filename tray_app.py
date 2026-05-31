"""Claude usage tray gauge — Windows 11 system-tray app.

Shows a double-ring icon (outer = 5-hour session, inner = weekly) that refreshes
on an interval. Hover for percentages + reset times; right-click for the menu.

Run:   pythonw tray_app.py     (no console)
       python  tray_app.py     (with console, for debugging)
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime
from typing import Optional

import pystray
from pystray import Menu, MenuItem

import autostart
import config as config_mod
import gauge as gauge_mod
import usage as usage_mod

# Windows consoles often default to cp932/Shift-JIS, which can't encode some
# characters and would crash print(). Force UTF-8 and never raise on encoding.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:  # noqa: BLE001 - older/odd streams; logging stays best-effort
        pass


def log(msg: str) -> None:
    """Print a timestamped line to the console (no-op window under pythonw)."""
    try:
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)
    except Exception:  # noqa: BLE001 - console logging must never crash the app
        pass


class TrayApp:
    def __init__(self) -> None:
        self.cfg = config_mod.load_config()
        self._stop = threading.Event()
        self._wake = threading.Event()  # forces an immediate refresh
        self._last: Optional[usage_mod.UsageSnapshot] = None

        self.icon = pystray.Icon(
            "claude_usage",
            icon=gauge_mod.render_double_ring(None, None, self.cfg, center_text="..."),
            title="Claude usage: loading…",
            menu=self._build_menu(),
        )

    # ----- menu -------------------------------------------------------------
    def _build_menu(self) -> Menu:
        return Menu(
            MenuItem("今すぐ更新 / Refresh now", self._on_refresh),
            Menu.SEPARATOR,
            MenuItem(
                "Windows起動時に自動起動 / Run at login",
                self._on_toggle_autostart,
                checked=lambda _i: autostart.is_enabled(),
            ),
            Menu.SEPARATOR,
            MenuItem("終了 / Quit", self._on_quit),
        )

    def _on_refresh(self, _icon=None, _item=None) -> None:
        # Guard the rate-limited oauth endpoint against rapid manual refreshes.
        if self._last is not None:
            age = (datetime.now().astimezone() - self._last.fetched_at).total_seconds()
            min_gap = 30 if self.cfg.get("source", "oauth") == "oauth" else 0
            if age < min_gap:
                self.icon.notify(f"{round(min_gap - age)}秒後に再試行できます", "Claude usage")
                return
        self._wake.set()

    def _on_toggle_autostart(self, _icon=None, _item=None) -> None:
        try:
            autostart.toggle()
        except Exception as exc:  # noqa: BLE001
            self.icon.notify(f"自動起動の切替に失敗: {exc}", "Claude usage")
        self.icon.update_menu()

    def _on_quit(self, _icon=None, _item=None) -> None:
        self._stop.set()
        self._wake.set()
        self.icon.stop()

    # ----- worker -----------------------------------------------------------
    def _apply(self, snap: usage_mod.UsageSnapshot) -> None:
        self._last = snap
        self.icon.icon = gauge_mod.make_icon(snap, self.cfg)
        self.icon.title = self._title_for(snap)
        if snap.ok:
            s = "?" if not snap.session or snap.session.pct is None else round(snap.session.pct * 100)
            w = "?" if not snap.weekly or snap.weekly.pct is None else round(snap.weekly.pct * 100)
            log(f"updated [{snap.source}] 5h={s}% week={w}%")
        else:
            log(f"update FAILED: {snap.error}")

    @staticmethod
    def _title_for(snap: usage_mod.UsageSnapshot) -> str:
        # Windows tray tooltip caps at 127 chars; keep it short.
        return "Claude usage\n" + usage_mod.tooltip_text(snap)

    def _worker(self, _icon=None) -> None:
        # The oauth endpoint is rate-limited; never poll it faster than 180s.
        floor = 180 if self.cfg.get("source", "oauth") == "oauth" else 30
        interval = max(floor, int(self.cfg.get("poll_interval_sec", 300)))
        log(f"worker started (source={self.cfg.get('source')}, interval={interval}s)")
        while not self._stop.is_set():
            snap = usage_mod.fetch_snapshot(self.cfg)
            self._apply(snap)
            # Wait for the interval, but wake early on manual refresh/quit.
            self._wake.wait(timeout=interval)
            self._wake.clear()
        log("worker stopped")

    def _setup(self, icon) -> None:
        # Called by pystray once the message loop is ready.
        icon.visible = True
        try:
            icon.notify("Claude usage 起動しました。トレイの '^' の中にアイコンがあります。",
                        "Claude usage")
        except Exception:  # noqa: BLE001
            pass
        threading.Thread(target=self._worker, daemon=True).start()

    def run(self) -> None:
        log("tray icon starting - look in the taskbar tray (click the '^' overflow arrow).")
        self.icon.run(setup=self._setup)
        log("tray app exited.")


def main() -> None:
    try:
        TrayApp().run()
    except Exception:  # noqa: BLE001 - surface startup crashes in the console
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
