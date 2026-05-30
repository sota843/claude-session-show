"""Usage snapshot for the tray gauge.

Two data sources:

* ``oauth``   (default, OFFICIAL) — calls the same private endpoint Claude Code's
              ``/usage`` command uses: ``GET https://api.anthropic.com/api/oauth/usage``.
              Returns real ``five_hour`` / ``seven_day`` utilisation percentages and
              reset times. Authenticated with the local OAuth access token from
              ``~/.claude/.credentials.json``. No budget guessing required.
* ``ccusage`` (estimate, fallback) — aggregates ``~/.claude/projects/**/*.jsonl``
              token counts via the ``ccusage`` CLI into 5-hour blocks / weeks and
              divides by a configured budget. Used automatically if the OAuth call
              fails (e.g. expired token) when ``fallback_to_ccusage`` is true.

The OAuth endpoint is undocumented and aggressively rate-limited; poll no faster
than ~180s and always send the ``User-Agent: claude-code/<version>`` header.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import config as config_mod

CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"


@dataclass
class Gauge:
    used: float          # for oauth: utilisation 0..100; for ccusage: token count
    budget: float        # for oauth: 100; for ccusage: token budget
    reset: Optional[datetime]  # local-tz aware

    @property
    def pct(self) -> Optional[float]:
        if self.budget and self.budget > 0:
            return max(0.0, min(1.0, self.used / self.budget))
        return None


@dataclass
class UsageSnapshot:
    session: Optional[Gauge]
    weekly: Optional[Gauge]
    error: Optional[str]
    fetched_at: datetime
    source: str = ""
    note: str = ""  # extra detail for tooltip (e.g. plan, opus weekly)

    @property
    def ok(self) -> bool:
        return self.error is None


def _parse_iso_to_local(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


# ---------------------------------------------------------------------------
# Source 1: official OAuth usage endpoint
# ---------------------------------------------------------------------------
_UA_CACHE: Optional[str] = None


def _user_agent(cfg: Dict[str, Any]) -> str:
    global _UA_CACHE
    override = cfg.get("user_agent")
    if override:
        return override
    if _UA_CACHE:
        return _UA_CACHE
    version = "2.1.0"
    exe = shutil.which("claude")
    if exe:
        try:
            out = subprocess.run([exe, "--version"], capture_output=True,
                                 text=True, timeout=15).stdout
            m = re.search(r"(\d+\.\d+\.\d+)", out)
            if m:
                version = m.group(1)
        except Exception:  # noqa: BLE001
            pass
    _UA_CACHE = f"claude-code/{version}"
    return _UA_CACHE


def _read_oauth_creds() -> Dict[str, Any]:
    with open(CREDENTIALS_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("claudeAiOauth", data)


def _window_gauge(window: Optional[Dict[str, Any]]) -> Optional[Gauge]:
    if not window or window.get("utilization") is None:
        return None
    reset = None
    if window.get("resets_at"):
        try:
            reset = _parse_iso_to_local(window["resets_at"])
        except ValueError:
            reset = None
    return Gauge(used=float(window["utilization"]), budget=100.0, reset=reset)


def _oauth_snapshot(cfg: Dict[str, Any], now: datetime) -> UsageSnapshot:
    creds = _read_oauth_creds()
    token = creds.get("accessToken")
    if not token:
        raise RuntimeError("no accessToken in credentials")

    expires_at = creds.get("expiresAt")
    if expires_at and time.time() * 1000 > float(expires_at):
        raise RuntimeError("oauth token expired (run Claude Code to refresh)")

    req = urllib.request.Request(
        OAUTH_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "User-Agent": _user_agent(cfg),
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)

    session = _window_gauge(data.get("five_hour"))
    weekly = _window_gauge(data.get("seven_day"))

    note_parts = []
    sub = creds.get("subscriptionType")
    if sub:
        note_parts.append(str(sub))
    opus = _window_gauge(data.get("seven_day_opus"))
    if opus is not None:
        note_parts.append(f"Opus wk {round(opus.pct * 100)}%")
    extra = data.get("extra_usage") or {}
    if extra.get("is_enabled") and extra.get("utilization") is not None:
        note_parts.append(f"extra {round(float(extra['utilization']))}%")

    return UsageSnapshot(session=session, weekly=weekly, error=None,
                         fetched_at=now, source="oauth", note=" · ".join(note_parts))


# ---------------------------------------------------------------------------
# Source 2: ccusage estimate (fallback)
# ---------------------------------------------------------------------------
def _run_ccusage(cfg: Dict[str, Any], subcommand: str) -> Dict[str, Any]:
    base = list(cfg["ccusage_cmd"])
    resolved = shutil.which(base[0])
    if resolved:
        base[0] = resolved
    elif not base[0].lower().endswith((".exe", ".cmd", ".bat")):
        raise FileNotFoundError(base[0])

    cmd = base + [subcommand, "--json"] + list(cfg.get("ccusage_extra_args", []))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, shell=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ccusage {subcommand} exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout)


def _ccusage_session_gauge(cfg: Dict[str, Any]) -> Gauge:
    blocks: List[Dict[str, Any]] = _run_ccusage(cfg, "blocks").get("blocks", [])
    active = next((b for b in blocks if b.get("isActive")), None)
    finished = [b for b in blocks if not b.get("isActive") and not b.get("isGap")]

    used = int(active["totalTokens"]) if active else 0
    reset = _parse_iso_to_local(active["endTime"]) if active and active.get("endTime") else None

    if cfg.get("budget_mode") == "fixed":
        budget = float(config_mod.fixed_budgets(cfg)["session"])
    else:
        peak = max((int(b.get("totalTokens", 0)) for b in finished), default=0)
        budget = float(max(peak, used, 1))
    return Gauge(used=used, budget=budget, reset=reset)


def _ccusage_weekly_gauge(cfg: Dict[str, Any]) -> Gauge:
    weeks: List[Dict[str, Any]] = _run_ccusage(cfg, "weekly").get("weekly", [])
    if not weeks:
        return Gauge(used=0, budget=1.0, reset=None)
    weeks_sorted = sorted(weeks, key=lambda w: w.get("period", ""))
    current, history = weeks_sorted[-1], weeks_sorted[:-1]

    used = int(current.get("totalTokens", 0))
    reset: Optional[datetime] = None
    if current.get("period"):
        try:
            reset = (datetime.strptime(current["period"], "%Y-%m-%d") + timedelta(days=7)).astimezone()
        except ValueError:
            reset = None

    if cfg.get("budget_mode") == "fixed":
        budget = float(config_mod.fixed_budgets(cfg)["weekly"])
    else:
        peak = max((int(w.get("totalTokens", 0)) for w in history), default=0)
        budget = float(max(peak, used, 1))
    return Gauge(used=used, budget=budget, reset=reset)


def _ccusage_snapshot(cfg: Dict[str, Any], now: datetime) -> UsageSnapshot:
    return UsageSnapshot(
        session=_ccusage_session_gauge(cfg),
        weekly=_ccusage_weekly_gauge(cfg),
        error=None, fetched_at=now, source="ccusage", note="estimate",
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def fetch_snapshot(cfg: Dict[str, Any]) -> UsageSnapshot:
    """Fetch a snapshot. Never raises; failures land in ``.error``."""
    now = datetime.now().astimezone()
    source = cfg.get("source", "oauth")

    if source == "oauth":
        try:
            return _oauth_snapshot(cfg, now)
        except Exception as oauth_exc:  # noqa: BLE001
            if not cfg.get("fallback_to_ccusage", True):
                return UsageSnapshot(None, None, f"oauth: {oauth_exc}", now, source="oauth")
            try:
                snap = _ccusage_snapshot(cfg, now)
                snap.note = f"estimate (oauth failed: {type(oauth_exc).__name__})"
                return snap
            except Exception as cc_exc:  # noqa: BLE001
                return UsageSnapshot(None, None, f"oauth: {oauth_exc}; ccusage: {cc_exc}", now)

    try:
        return _ccusage_snapshot(cfg, now)
    except FileNotFoundError as exc:
        return UsageSnapshot(None, None, f"ccusage not found: {exc}", now)
    except Exception as exc:  # noqa: BLE001
        return UsageSnapshot(None, None, f"{type(exc).__name__}: {exc}", now)


def _fmt_pct(g: Optional[Gauge]) -> str:
    if g is None or g.pct is None:
        return "??%"
    return f"{round(g.pct * 100)}%"


def _fmt_reset(g: Optional[Gauge]) -> str:
    if g is None or g.reset is None:
        return "--"
    return g.reset.strftime("%a %H:%M")


def tooltip_text(snap: UsageSnapshot) -> str:
    if not snap.ok:
        return f"unavailable\n{snap.error}"
    s, w = snap.session, snap.weekly
    lines = [
        f"5h:  {_fmt_pct(s)}  (reset {_fmt_reset(s)})",
        f"Week: {_fmt_pct(w)}  (reset {_fmt_reset(w)})",
    ]
    tag = snap.note or snap.source
    if tag:
        lines.append(tag)
    return "\n".join(lines)


if __name__ == "__main__":
    cfg = config_mod.load_config()
    snap = fetch_snapshot(cfg)
    print(f"[source={snap.source}]")
    print(tooltip_text(snap))
    if snap.ok:
        for label, g in (("session", snap.session), ("weekly", snap.weekly)):
            if g is None:
                print(f"  {label}: n/a")
                continue
            pct = None if g.pct is None else round(g.pct * 100, 1)
            print(f"  {label}: used={g.used} budget={int(g.budget)} pct={pct} reset={g.reset}")
