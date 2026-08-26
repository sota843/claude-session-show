"""Claude usage tray gauge — Windows 11 system-tray app.

Shows a double-ring icon (outer = 5-hour session, inner = weekly) that refreshes
on an interval. Hover for percentages + reset times; right-click for the menu.

Run:   pythonw tray_app.py     (no console)
       python  tray_app.py     (with console, for debugging)
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime
from typing import Optional

import pystray
from pystray import Menu, MenuItem

import appdata
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
    """Timestamped line to the console *and* to the log file - under pythonw the
    console does not exist, so the file is the only way to see what happened."""
    appdata.log(msg)


class TrayApp:
    def __init__(self) -> None:
        self.cfg = config_mod.load_config()
        self._stop = threading.Event()
        self._wake = threading.Event()  # forces an immediate refresh
        self._last: Optional[usage_mod.UsageSnapshot] = None
        # Last reading that actually succeeded, restored from disk so a login
        # that starts before the network (or during a refresh backoff) still
        # shows the previous value instead of an empty "?" ring.
        self._last_good = usage_mod.snapshot_from_dict(
            appdata.load_state().get("last_snapshot"))

        icon_img = (gauge_mod.make_icon(self._last_good, self.cfg) if self._last_good
                    else gauge_mod.render_double_ring(None, None, self.cfg, center_text="..."))
        self.icon = pystray.Icon(
            "claude_usage",
            icon=icon_img,
            title="Claude usage: loading…",
            menu=self._build_menu(),
        )

    # ----- menu -------------------------------------------------------------
    def _build_menu(self) -> Menu:
        return Menu(
            MenuItem("今すぐ更新 / Refresh now", self._on_refresh),
            MenuItem("トークンを再取得 / Force token refresh", self._on_force_token),
            MenuItem("ログを開く / Open log", self._on_open_log),
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

    def _on_force_token(self, _icon=None, _item=None) -> None:
        """Retry the OAuth refresh immediately, ignoring the backoff window."""
        def work() -> None:
            try:
                usage_mod._refresh_oauth_token(
                    usage_mod._read_oauth_creds(), self.cfg, force=True)
                self.icon.notify("トークンを更新しました", "Claude usage")
                self._wake.set()
            except Exception as exc:  # noqa: BLE001
                log(f"forced token refresh failed: {exc}")
                self.icon.notify(f"更新失敗: {str(exc)[:150]}", "Claude usage")

        threading.Thread(target=work, daemon=True).start()

    def _on_open_log(self, _icon=None, _item=None) -> None:
        try:
            os.startfile(appdata.LOG_PATH)  # noqa: S606 - user-initiated
        except OSError as exc:
            self.icon.notify(f"ログを開けません: {exc}", "Claude usage")

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
        if snap.ok:
            self._last_good = snap
            appdata.update_state(last_snapshot=usage_mod.snapshot_to_dict(snap))
            self.icon.icon = gauge_mod.make_icon(snap, self.cfg)
            self.icon.title = self._title_for(snap)
            s = "?" if not snap.session or snap.session.pct is None else round(snap.session.pct * 100)
            w = "?" if not snap.weekly or snap.weekly.pct is None else round(snap.weekly.pct * 100)
            log(f"updated [{snap.source}] 5h={s}% week={w}%")
            return

        log(f"update FAILED: {snap.error}")
        if self._last_good is not None:
            # Keep the last known rings on screen, but say plainly that they are old.
            self.icon.icon = gauge_mod.make_icon(self._last_good, self.cfg)
            self.icon.title = self._stale_title(self._last_good, snap)
        else:
            self.icon.icon = gauge_mod.make_icon(snap, self.cfg)
            self.icon.title = self._title_for(snap)

    @staticmethod
    def _title_for(snap: usage_mod.UsageSnapshot) -> str:
        # Windows tray tooltip caps at 127 chars; keep it short.
        return ("Claude usage\n" + usage_mod.tooltip_text(snap))[:127]

    @staticmethod
    def _stale_title(good: usage_mod.UsageSnapshot,
                     failed: usage_mod.UsageSnapshot) -> str:
        age_min = round((datetime.now().astimezone() - good.fetched_at).total_seconds() / 60)
        age = f"{age_min}分前" if age_min < 90 else f"{round(age_min / 60)}時間前"
        # 127 chars is the hard Windows tooltip cap: keep the numbers and the
        # stale warning, then spend whatever is left on the reason.
        head = usage_mod.tooltip_text(good).split("\n")[:2]  # 5h + week lines
        title = "\n".join(["Claude usage", *head, f"⚠ 取得失敗 / 表示は{age}の値"])
        reason = (failed.error or "").split(";")[0].strip()
        room = 127 - len(title) - 1
        if reason and room > 12:
            title += "\n" + reason[:room]
        return title[:127]

    def _sleep(self, seconds: float) -> None:
        """Wait out the poll interval, waking early on refresh/quit - or as soon
        as credentials.json changes, i.e. the moment Claude Code hands us a fresh
        token. That turns 'works again once I open Claude Code' into seconds."""
        creds_mtime = self._creds_mtime()
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self._wake.wait(timeout=min(5.0, remaining)):
                return
            mtime = self._creds_mtime()
            if mtime and creds_mtime and mtime != creds_mtime:
                log("credentials.json changed (Claude Code refreshed) - updating now")
                return

    @staticmethod
    def _creds_mtime() -> Optional[float]:
        try:
            return os.path.getmtime(usage_mod.CREDENTIALS_PATH)
        except OSError:
            return None

    def _worker(self, _icon=None) -> None:
        # The oauth endpoint is rate-limited; never poll it faster than 60s.
        floor = 60 if self.cfg.get("source", "oauth") == "oauth" else 30
        interval = max(floor, int(self.cfg.get("poll_interval_sec", 300)))
        log(f"worker started (source={self.cfg.get('source')}, interval={interval}s)")
        while not self._stop.is_set():
            snap = usage_mod.fetch_snapshot(self.cfg)
            self._apply(snap)
            # After a failure retry sooner (login races the network), but never
            # faster than the floor - the refresh itself is backed off separately.
            self._sleep(interval if snap.ok else max(floor, min(interval, 120)))
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
